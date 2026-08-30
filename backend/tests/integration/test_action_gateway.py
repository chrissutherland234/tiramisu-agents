"""Action proposal durability and tenant isolation against PostgreSQL."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.actions.gateway import ActionGateway, ActionPersistenceConflict
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.decisions import ActionProposal, AgentDecision, DecisionStatus
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.actions import (
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.temporal.activities.action_gateway import (
    ActionGatewayActivities,
    PersistActionsCommand,
)

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        for model in (ApprovalRequest, ActionPolicyRecord, ActionRevision, ActionRequest):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.execute(delete(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id))
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(delete(ProcessInstance).where(ProcessInstance.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_gateway_is_idempotent_hash_bound_and_tenant_isolated() -> None:
    runtime_url = os.getenv(
        "TIRAMISU_DATABASE_URL",
        "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test",
    )
    migration_url = os.getenv(
        "TIRAMISU_MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
    )
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    definition = ProcessDefinitionRegistry.from_yaml_files(
        [Path("process_definitions/examples/enquiry_to_booking.v1.yaml")]
    ).get("enquiry_to_booking", "1")
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    turn_id = uuid4()
    event = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="stub.website",
        source_event_id=f"source-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(
            ExternalReference(
                provider="stub.website",
                resource_type="enquiry",
                external_id=f"enquiry-{uuid4()}",
            ),
        ),
    )
    decision = AgentDecision(
        based_on_event_ids=(event.event_id,),
        status=DecisionStatus.ACTIVE,
        actions=(
            ActionProposal(
                logical_action_key="reply_1",
                action_type="send_message",
                parameters={"recipient": "customer@example.test", "body": "Hello"},
                rationale="Respond to the enquiry.",
            ),
            ActionProposal(
                logical_action_key="availability_1",
                action_type="find_available_slots",
                parameters={"days": 7},
                rationale="Find suitable times.",
            ),
        ),
    )
    gateway = ActionGateway()

    try:
        async with admin_factory.begin() as session:
            session.add_all(
                [
                    Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Gateway Tenant"),
                    Tenant(
                        id=other_tenant_id,
                        slug=f"tenant-{other_tenant_id}",
                        name="Other Tenant",
                    ),
                ]
            )
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=ProcessBootstrap(
                    process_type=definition.id,
                    definition_version=definition.version,
                    extension_manifest_hash="a" * 64,
                ),
            )
        assert ingested.process_instance_id is not None

        async with runtime_factory.begin() as session:
            first = await gateway.persist_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                agent_turn_id=turn_id,
                process_definition_version=definition.version,
                decision=decision,
                policy=definition.action_policy(),
            )
        async with runtime_factory.begin() as session:
            retried = await gateway.persist_decision(
                session,
                tenant_id=tenant_id,
                process_instance_id=ingested.process_instance_id,
                agent_turn_id=turn_id,
                process_definition_version=definition.version,
                decision=decision,
                policy=definition.action_policy(),
            )

        assert retried == first
        assert first[0].outcome is PermissionOutcome.REQUIRE_APPROVAL
        assert first[0].approval_request_id is not None
        assert first[1].outcome is PermissionOutcome.ALLOW
        assert first[1].approval_request_id is None

        activity_result = await ActionGatewayActivities(
            runtime_factory,
            ProcessDefinitionRegistry([definition]),
        ).persist_agent_actions(
            PersistActionsCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id=definition.id,
                process_definition_version=definition.version,
                agent_turn_id=str(turn_id),
                event_ids=(str(event.event_id),),
                workflow_now=datetime.now(UTC),
                decision_json=decision.model_dump_json(),
            )
        )
        assert len(json.loads(activity_result.actions_json)) == 2

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            assert await session.scalar(select(func.count()).select_from(ActionRequest)) == 2
            assert await session.scalar(select(func.count()).select_from(ActionRevision)) == 2
            assert await session.scalar(select(func.count()).select_from(ActionPolicyRecord)) == 2
            assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 1
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, other_tenant_id)
            assert await session.scalar(select(func.count()).select_from(ActionRequest)) == 0

        changed = decision.model_copy(
            update={
                "actions": (
                    decision.actions[0].model_copy(
                        update={"parameters": {"recipient": "other@example.test", "body": "Hello"}}
                    ),
                )
            }
        )
        with pytest.raises(ActionPersistenceConflict, match="another payload"):
            async with runtime_factory.begin() as session:
                await gateway.persist_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=ingested.process_instance_id,
                    agent_turn_id=turn_id,
                    process_definition_version=definition.version,
                    decision=changed,
                    policy=definition.action_policy(),
                )
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await _delete_tenant_data(admin_factory, other_tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
