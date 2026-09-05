"""Bounded agent Activity integration against tenant-scoped PostgreSQL context."""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import ApplicationError
from tiramisu_agents.agents.context import PostgresAgentContextLoader
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant, TenantSafetyEvent
from tiramisu_agents.db.models.usage import ModelUsageLedger
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.security.tenancy import TenantSafetyService
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities, AgentTurnCommand
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE
from tiramisu_agents.testkit.scripted_agent import ScriptedAgent

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        await session.execute(
            delete(ModelUsageLedger).where(ModelUsageLedger.tenant_id == tenant_id)
        )
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
async def test_activity_repairs_then_rejects_out_of_policy_decisions_with_one_snapshot() -> None:
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
    deployment = load_fictional_deployment()
    registry = deployment.registry
    definition = deployment.definition
    compatibility = DeploymentCompatibility(
        client_pack_fingerprint="b" * 64,
        extension_manifest_hash="a" * 64,
        definition_fingerprints={(definition.id, definition.version): definition.fingerprint()},
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
        facts=(
            FactObservation(
                key="enquiry.received",
                kind=FactKind.AUTHORITATIVE,
                value=True,
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
    repaired_decision = valid_decision.model_copy(update={"decision_id": uuid4()})
    scripted_agent = ScriptedAgent(
        [
            valid_decision,
            invalid_decision,
            invalid_decision,
            repaired_decision,
            invalid_decision,
            invalid_decision,
            invalid_decision,
        ]
    )
    activities = AgentTurnActivities(
        runtime_factory,
        registry,
        scripted_agent,
        compatibility=compatibility,
        deployment_release=TEST_DEPLOYMENT_RELEASE,
    )

    try:
        async with admin_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"tenant-{tenant_id}",
                    name="Test Tenant",
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                )
            )
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version=definition.version,
                    extension_manifest_hash="a" * 64,
                    client_pack_fingerprint="b" * 64,
                    process_definition_fingerprint=definition.fingerprint(),
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    deployment_release_fingerprint=TEST_DEPLOYMENT_RELEASE.release_fingerprint,
                    temporal_task_queue=TEST_DEPLOYMENT_RELEASE.temporal_task_queue,
                ),
            )
        assert ingested.process_instance_id is not None
        command = AgentTurnCommand(
            tenant_id=str(tenant_id),
            process_instance_id=str(ingested.process_instance_id),
            process_definition_id="enquiry_to_booking",
            process_definition_version=definition.version,
            turn_id=str(uuid4()),
            event_ids=(str(event.event_id),),
            workflow_now=datetime.now(UTC),
        )

        result = await activities.run_agent_turn(command)
        assert AgentDecision.model_validate_json(result.decision_json) == valid_decision
        assert result.proposal_attempt_count == 1
        assert (
            scripted_agent.turn_inputs[0].events[0].process_instance_id
            == ingested.process_instance_id
        )
        assert scripted_agent.turn_inputs[0].workflow_now == command.workflow_now
        assert "Allowed action types" in scripted_agent.turn_inputs[0].instructions

        repaired = await activities.run_agent_turn(command)
        assert AgentDecision.model_validate_json(repaired.decision_json) == repaired_decision
        assert repaired.proposal_attempt_count == 3
        assert scripted_agent.turn_inputs[1] is scripted_agent.turn_inputs[2]
        assert scripted_agent.turn_inputs[2] is scripted_agent.turn_inputs[3]
        corrections = scripted_agent.corrections[2:4]
        assert all(correction is not None for correction in corrections)
        assert [
            correction.correction_attempt if correction is not None else None
            for correction in corrections
        ] == [1, 2]
        assert all(
            correction is not None and correction.rejected_decision is invalid_decision
            for correction in corrections
        )
        assert all(
            correction is not None
            and correction.validation_error == "action type is not allowed: delete_everything"
            for correction in corrections
        )

        with pytest.raises(ApplicationError) as raised:
            await activities.run_agent_turn(command)
        assert raised.value.non_retryable is True
        assert raised.value.type == "DecisionRejected"
        assert len(scripted_agent.turn_inputs) == 7
        assert scripted_agent.turn_inputs[4] is scripted_agent.turn_inputs[5]
        assert scripted_agent.turn_inputs[5] is scripted_agent.turn_inputs[6]
        assert [
            item.correction_attempt if item is not None else None
            for item in scripted_agent.corrections[4:]
        ] == [None, 1, 2]

        blocked_agent = ScriptedAgent([valid_decision])
        context_limited_activities = AgentTurnActivities(
            runtime_factory,
            registry,
            blocked_agent,
            compatibility=compatibility,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
            context_loader=PostgresAgentContextLoader(max_agent_context_bytes=1),
        )
        with pytest.raises(ApplicationError) as context_limited:
            await context_limited_activities.run_agent_turn(command)
        assert context_limited.value.non_retryable is True
        assert context_limited.value.type == "AgentContextLimitExceeded"
        assert blocked_agent.turn_inputs == []

        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.authoritative_facts = {f"existing.fact_{index}": index for index in range(500)}
        projection_blocked_agent = ScriptedAgent([valid_decision])
        projection_limited_activities = AgentTurnActivities(
            runtime_factory,
            registry,
            projection_blocked_agent,
            compatibility=compatibility,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        )
        with pytest.raises(ApplicationError) as projection_limited:
            await projection_limited_activities.run_agent_turn(command)
        assert projection_limited.value.non_retryable is True
        assert projection_limited.value.type == "AgentContextLimitExceeded"
        assert "process fact projection" in str(projection_limited.value)
        assert projection_blocked_agent.turn_inputs == []
        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.authoritative_facts = {}

        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.client_pack_fingerprint = "d" * 64
        prior_turn_count = len(scripted_agent.turn_inputs)
        with pytest.raises(ApplicationError) as incompatible:
            await activities.run_agent_turn(command)
        assert incompatible.value.non_retryable is True
        assert incompatible.value.type == "DeploymentCompatibilityError"
        assert len(scripted_agent.turn_inputs) == prior_turn_count
        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.client_pack_fingerprint = "b" * 64

        async with admin_factory.begin() as session:
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.created_at = command.workflow_now - timedelta(
                days=definition.limits.maximum_process_lifetime_days
            )
        prior_turn_count = len(scripted_agent.turn_inputs)
        with pytest.raises(ApplicationError) as expired:
            await activities.run_agent_turn(command)
        assert expired.value.non_retryable is True
        assert expired.value.type == "ProcessLifetimeExceeded"
        assert "process lifetime ended" in str(expired.value)
        assert len(scripted_agent.turn_inputs) == prior_turn_count

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
