"""Bounded agent Activity integration against tenant-scoped PostgreSQL context."""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import ApplicationError
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant, TenantSafetyEvent
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.security.tenancy import TenantSafetyService
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities, AgentTurnCommand
from tiramisu_agents.testkit.scripted_agent import ScriptedAgent

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        await session.execute(delete(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id))
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(delete(ProcessInstance).where(ProcessInstance.tenant_id == tenant_id))
        await session.execute(
            delete(TenantSafetyEvent).where(TenantSafetyEvent.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_activity_loads_context_and_rejects_out_of_policy_decision() -> None:
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
    registry = ProcessDefinitionRegistry.from_yaml_files(
        [Path("process_definitions/examples/enquiry_to_booking.v1.yaml")]
    )
    tenant_id = uuid4()
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
    valid_decision = AgentDecision(
        based_on_event_ids=(event.event_id,),
        status=DecisionStatus.WAITING,
        wake_conditions=(EventWakeCondition(event_type="customer.email_received"),),
    )
    invalid_decision = AgentDecision(
        based_on_event_ids=(event.event_id,),
        status=DecisionStatus.ACTIVE,
        actions=(
            ActionProposal(
                logical_action_key="unsafe-action",
                action_type="delete_everything",
                rationale="Exercise deterministic policy rejection.",
            ),
        ),
    )
    scripted_agent = ScriptedAgent([valid_decision, invalid_decision])
    activities = AgentTurnActivities(runtime_factory, registry, scripted_agent)

    try:
        async with admin_factory.begin() as session:
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Test Tenant"))
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                ),
            )
        assert ingested.process_instance_id is not None
        command = AgentTurnCommand(
            tenant_id=str(tenant_id),
            process_instance_id=str(ingested.process_instance_id),
            process_definition_id="enquiry_to_booking",
            process_definition_version="1",
            turn_id=str(uuid4()),
            event_ids=(str(event.event_id),),
            workflow_now=datetime.now(UTC),
        )

        result = await activities.run_agent_turn(command)
        assert AgentDecision.model_validate_json(result.decision_json) == valid_decision
        assert (
            scripted_agent.turn_inputs[0].events[0].process_instance_id
            == ingested.process_instance_id
        )
        assert "Allowed action types" in scripted_agent.turn_inputs[0].instructions

        with pytest.raises(ApplicationError) as raised:
            await activities.run_agent_turn(command)
        assert raised.value.non_retryable is True
        assert raised.value.type == "DecisionRejected"

        async with admin_factory.begin() as session:
            await TenantSafetyService().set_status(
                session,
                tenant_id=tenant_id,
                actor_id=uuid4(),
                new_status="suspended",
                reason="Stop model calls during incident response",
            )
        prior_turn_count = len(scripted_agent.turn_inputs)
        with pytest.raises(ApplicationError) as suspended:
            await activities.run_agent_turn(command)
        assert suspended.value.non_retryable is True
        assert suspended.value.type == "TenantSuspended"
        assert len(scripted_agent.turn_inputs) == prior_turn_count
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
