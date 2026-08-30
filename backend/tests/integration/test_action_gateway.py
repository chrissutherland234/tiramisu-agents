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
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.db.models.actions import (
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.reviews.service import ReviewConflict, ReviewService
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
        for model in (
            ApprovalDecision,
            ReviewMessage,
            ReviewThread,
            ApprovalRequest,
            ActionPolicyRecord,
            ActionRevision,
            ActionRequest,
        ):
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
            ActionProposal(
                logical_action_key="booking_1",
                action_type="propose_booking",
                parameters={"slot": "2026-09-02T10:00:00+00:00"},
                rationale="Offer an available time.",
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
        assert first[0].review_thread_id is not None
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
        assert len(json.loads(activity_result.actions_json)) == 3

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            assert await session.scalar(select(func.count()).select_from(ActionRequest)) == 3
            assert await session.scalar(select(func.count()).select_from(ActionRevision)) == 3
            assert await session.scalar(select(func.count()).select_from(ActionPolicyRecord)) == 3
            assert await session.scalar(select(func.count()).select_from(ApprovalRequest)) == 2
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, other_tenant_id)
            assert await session.scalar(select(func.count()).select_from(ActionRequest)) == 0

        actor_id = uuid4()
        comment = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[0].review_thread_id,
            action_request_id=first[0].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.COMMENT,
            actor_id=actor_id,
            message="Could this be a little warmer?",
        )
        review_service = ReviewService()
        async with runtime_factory.begin() as session:
            comment_result = await review_service.apply(session, comment)
        async with runtime_factory.begin() as session:
            assert await review_service.apply(session, comment) == comment_result
        assert comment_result.thread_status == "open"

        wrong_hash = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[0].review_thread_id,
            action_request_id=first[0].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.APPROVE,
            actor_id=actor_id,
            expected_payload_hash="0" * 64,
        )
        with pytest.raises(ReviewConflict, match="payload hash"):
            async with runtime_factory.begin() as session:
                await review_service.apply(session, wrong_hash)

        approve = wrong_hash.model_copy(
            update={"command_id": uuid4(), "expected_payload_hash": first[0].payload_hash}
        )
        async with runtime_factory.begin() as session:
            approved = await review_service.apply(session, approve)
        async with runtime_factory.begin() as session:
            assert await review_service.apply(session, approve) == approved
        assert approved.approval_status == "approved"
        assert approved.action_status == "approved"

        late_revision = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[0].review_thread_id,
            action_request_id=first[0].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.REQUEST_REVISION,
            actor_id=actor_id,
            message="Actually, offer Tuesday instead.",
        )
        with pytest.raises(ReviewConflict, match="no longer pending"):
            async with runtime_factory.begin() as session:
                await review_service.apply(session, late_revision)

        assert first[2].review_thread_id is not None
        request_revision = ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=ingested.process_instance_id,
            review_thread_id=first[2].review_thread_id,
            action_request_id=first[2].action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.REQUEST_REVISION,
            actor_id=actor_id,
            message="Offer Tuesday afternoon instead.",
        )
        async with runtime_factory.begin() as session:
            revision_result = await review_service.apply(session, request_revision)
        assert revision_result.thread_status == "revision_requested"
        assert revision_result.approval_status == "superseded"
        assert revision_result.action_status == "superseded"

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
