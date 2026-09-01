"""Focused PostgreSQL tests for lifecycle controls introduced by hardening."""

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.actions.gateway import (
    ActionGateway,
    ActionPersistenceConflict,
    CommunicationPolicy,
)
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.decisions import ActionProposal, AgentDecision, DecisionStatus
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.ports.actions import ProviderActionRequest, ProviderActionResult
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential, TenantSafetyEvent
from tiramisu_agents.db.session import (
    create_engine,
    create_session_factory,
    set_tenant_context,
)
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.processes.control import (
    InterventionInput,
    ProcessControlInput,
    ProcessControlService,
    ProcessControlType,
)
from tiramisu_agents.processes.state import ProcessStateService
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


@dataclass(frozen=True, slots=True)
class _TestContext:
    runtime_factory: async_sessionmaker[AsyncSession]
    admin_factory: async_sessionmaker[AsyncSession]
    tenant_id: UUID
    process_id: UUID
    enquiry: CanonicalEvent
    compatibility: DeploymentCompatibility


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    models = (
        ApprovalDecision,
        ReviewMessage,
        ReviewThread,
        ApprovalRequest,
        ActionReconciliationDecision,
        ActionAttempt,
        ActionPolicyRecord,
        ActionRevision,
        ActionRequest,
        ProcessStateRevision,
        ProcessControlCommand,
        ProcessIntervention,
        OutboxMessage,
        EventInbox,
        ExternalCorrelation,
        TenantCredential,
        TenantSafetyEvent,
        ProcessInstance,
    )
    async with admin_factory.begin() as session:
        for model in models:
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@asynccontextmanager
async def _process_context() -> AsyncGenerator[_TestContext]:
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
    tenant_id = uuid4()
    enquiry = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="hardening.test",
        source_event_id=f"enquiry-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(
            ExternalReference(
                provider="hardening.test",
                resource_type="enquiry",
                external_id=f"enquiry-{uuid4()}",
            ),
        ),
    )
    definition = load_fictional_deployment().definition
    compatibility = DeploymentCompatibility(
        client_pack_fingerprint="b" * 64,
        extension_manifest_hash="a" * 64,
        definition_fingerprints={(definition.id, definition.version): definition.fingerprint()},
    )
    try:
        async with admin_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"tenant-{tenant_id}",
                    name="Hardening",
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                )
            )
        async with runtime_factory.begin() as session:
            result = await EventIngestionService().ingest(
                session,
                enquiry,
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                    client_pack_fingerprint="b" * 64,
                    process_definition_fingerprint=definition.fingerprint(),
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    deployment_release_fingerprint=TEST_DEPLOYMENT_RELEASE.release_fingerprint,
                    temporal_task_queue=TEST_DEPLOYMENT_RELEASE.temporal_task_queue,
                ),
            )
        assert result.process_instance_id is not None
        yield _TestContext(
            runtime_factory=runtime_factory,
            admin_factory=admin_factory,
            tenant_id=tenant_id,
            process_id=result.process_instance_id,
            enquiry=enquiry,
            compatibility=compatibility,
        )
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_interventions_controls_and_takeover_are_durable_and_idempotent() -> None:
    async with _process_context() as context:
        service = ProcessControlService()
        intervention_id = uuid4()
        async with context.runtime_factory.begin() as session:
            intervention = await service.record_intervention(
                session,
                InterventionInput(
                    intervention_id=intervention_id,
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=uuid4(),
                    kind="turn_failure",
                    error_type="DecisionRejected",
                    error="model output had no progress path",
                    event_ids=(context.enquiry.event_id,),
                ),
            )
        assert intervention.status == "open"

        async with context.runtime_factory.begin() as session:
            await set_tenant_context(session, context.tenant_id)
            process = await session.get(ProcessInstance, context.process_id)
            assert process is not None
            assert process.status == "review"
            assert process.current_wake_conditions == [{"type": "human", "interaction": "operator"}]

        retry = ProcessControlInput(
            command_id=uuid4(),
            tenant_id=context.tenant_id,
            process_instance_id=context.process_id,
            actor_id=uuid4(),
            command_type=ProcessControlType.RETRY,
            reason="Corrected prompt is ready",
            intervention_id=intervention_id,
        )

        async def apply_retry() -> ProcessControlCommand:
            async with context.runtime_factory.begin() as session:
                return await service.apply_control(session, retry)

        first, repeated = await asyncio.gather(apply_retry(), apply_retry())
        assert first.id == repeated.id == retry.command_id

        takeover = ProcessControlInput(
            command_id=uuid4(),
            tenant_id=context.tenant_id,
            process_instance_id=context.process_id,
            actor_id=uuid4(),
            command_type=ProcessControlType.TAKEOVER,
            reason="Operator is taking over this case",
        )
        async with context.runtime_factory.begin() as session:
            await service.apply_control(session, takeover)
        async with context.runtime_factory.begin() as session:
            await service.record_intervention(
                session,
                InterventionInput(
                    intervention_id=uuid4(),
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=uuid4(),
                    kind="turn_failure",
                    error_type="InFlightTurnFailed",
                    error="turn lost a race with takeover",
                ),
            )

        async with context.runtime_factory.begin() as session:
            await set_tenant_context(session, context.tenant_id)
            process = await session.get(ProcessInstance, context.process_id)
            original = await session.get(ProcessIntervention, intervention_id)
            outbox_types = (
                await session.scalars(
                    select(OutboxMessage.message_type)
                    .where(OutboxMessage.process_instance_id == context.process_id)
                    .order_by(OutboxMessage.created_at)
                )
            ).all()
            assert process is not None
            assert original is not None
            assert process.status == "paused"
            assert original.status == "resolved"
            assert original.resolved_by_command_id == retry.command_id
            assert outbox_types.count("temporal.process_control") == 2


@pytest.mark.asyncio
async def test_terminal_late_event_is_recorded_without_restarting_closed_workflow() -> None:
    async with _process_context() as context:
        async with context.runtime_factory.begin() as session:
            await set_tenant_context(session, context.tenant_id)
            process = await session.get(ProcessInstance, context.process_id)
            assert process is not None
            process.status = "completed"

        late_event = CanonicalEvent(
            tenant_id=context.tenant_id,
            process_instance_id=context.process_id,
            event_type="customer.email_received",
            source="hardening.test",
            source_event_id=f"late-{uuid4()}",
            occurred_at=datetime.now(UTC),
        )
        async with context.runtime_factory.begin() as session:
            result = await EventIngestionService().ingest(session, late_event)
        assert result.correlation_status == "matched"
        assert result.correlation_reason == "terminal_process_record_only"
        assert result.outbox_message_id is None


@pytest.mark.asyncio
async def test_escalation_without_an_explicit_wake_gets_a_durable_operator_wake() -> None:
    async with _process_context() as context:
        decision = AgentDecision(
            based_on_event_ids=(context.enquiry.event_id,),
            status=DecisionStatus.ESCALATED,
        )
        async with context.runtime_factory.begin() as session:
            applied = await ProcessStateService().apply_decision(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                decision=decision,
            )

        assert applied.status.value == "review"
        assert [wake.model_dump(mode="json") for wake in applied.wake_conditions] == [
            {"type": "human", "interaction": "operator"}
        ]


@dataclass(slots=True)
class _BlockingAdapter:
    id: str = "blocking.actions.v1"
    guarantees_idempotency: bool = True
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[ProviderActionRequest] = field(
        default_factory=lambda: list[ProviderActionRequest]()
    )

    async def execute(self, request: ProviderActionRequest) -> ProviderActionResult:
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        return ProviderActionResult(provider_reference="blocking:done", result={"ok": True})

    async def lookup(self, idempotency_key: str) -> ProviderActionResult | None:
        del idempotency_key
        return None


@pytest.mark.asyncio
async def test_takeover_serializes_with_the_final_provider_execution_fence() -> None:
    async with _process_context() as context:
        definition = load_fictional_deployment().definition
        decision = AgentDecision(
            based_on_event_ids=(context.enquiry.event_id,),
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="availability",
                    action_type="find_available_slots",
                    parameters={"days": 7},
                    rationale="Find slots before takeover.",
                ),
            ),
        )
        async with context.runtime_factory.begin() as session:
            actions = await ActionGateway().persist_decision(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                process_definition_version=definition.version,
                decision=decision,
                policy=definition.action_policy(),
            )

        adapter = _BlockingAdapter()
        executor = ActionExecutor(
            context.runtime_factory,
            ActionAdapterRegistry({"find_available_slots": adapter}),
            context.compatibility,
            TEST_DEPLOYMENT_RELEASE,
        )
        execution = asyncio.create_task(
            executor.execute(
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                action_request_id=actions[0].action_request_id,
                revision=1,
            )
        )
        await asyncio.wait_for(adapter.entered.wait(), timeout=2)

        takeover_command = ProcessControlInput(
            command_id=uuid4(),
            tenant_id=context.tenant_id,
            process_instance_id=context.process_id,
            actor_id=uuid4(),
            command_type=ProcessControlType.TAKEOVER,
            reason="Stop after the already-fenced provider request",
        )

        async def take_over() -> None:
            async with context.runtime_factory.begin() as session:
                await ProcessControlService().apply_control(session, takeover_command)

        takeover = asyncio.create_task(take_over())
        await asyncio.sleep(0.05)
        assert takeover.done() is False
        adapter.release.set()
        result = await asyncio.wait_for(execution, timeout=2)
        await asyncio.wait_for(takeover, timeout=2)

        assert result.status.value == "succeeded"
        assert len(adapter.requests) == 1
        async with context.runtime_factory.begin() as session:
            await set_tenant_context(session, context.tenant_id)
            process = await session.get(ProcessInstance, context.process_id)
            assert process is not None
            assert process.status == "paused"


@pytest.mark.asyncio
async def test_communication_limits_reset_only_after_a_configured_reply() -> None:
    async with _process_context() as context:
        definition = load_fictional_deployment().definition
        gateway = ActionGateway()
        policy = CommunicationPolicy(
            outbound_action_types=frozenset({"send_message"}),
            reply_event_types=frozenset({"customer.email_received"}),
            max_follow_ups_without_reply=1,
            minimum_follow_up_interval=timedelta(hours=1),
        )

        def message_decision(key: str) -> AgentDecision:
            return AgentDecision(
                based_on_event_ids=(),
                status=DecisionStatus.ACTIVE,
                actions=(
                    ActionProposal(
                        logical_action_key=key,
                        action_type="send_message",
                        parameters={"recipient": "customer@example.test", "body": key},
                        rationale="Follow up within deterministic limits.",
                    ),
                ),
            )

        now = datetime.now(UTC)
        async with context.runtime_factory.begin() as session:
            await gateway.persist_decision(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                process_definition_version=definition.version,
                decision=message_decision("first"),
                policy=definition.action_policy(),
                communication_policy=policy,
                workflow_now=now,
            )
        interval_policy = CommunicationPolicy(
            outbound_action_types=policy.outbound_action_types,
            reply_event_types=policy.reply_event_types,
            max_follow_ups_without_reply=3,
            minimum_follow_up_interval=policy.minimum_follow_up_interval,
        )
        with pytest.raises(ActionPersistenceConflict, match="minimum follow-up interval"):
            async with context.runtime_factory.begin() as session:
                await gateway.persist_decision(
                    session,
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=uuid4(),
                    process_definition_version=definition.version,
                    decision=message_decision("too-soon"),
                    policy=definition.action_policy(),
                    communication_policy=interval_policy,
                    workflow_now=now,
                )
        with pytest.raises(ActionPersistenceConflict, match="maximum follow-ups"):
            async with context.runtime_factory.begin() as session:
                await gateway.persist_decision(
                    session,
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=uuid4(),
                    process_definition_version=definition.version,
                    decision=message_decision("blocked"),
                    policy=definition.action_policy(),
                    communication_policy=policy,
                    workflow_now=now + timedelta(hours=2),
                )

        reply = CanonicalEvent(
            tenant_id=context.tenant_id,
            process_instance_id=context.process_id,
            event_type="customer.email_received",
            source="hardening.test",
            source_event_id=f"reply-{uuid4()}",
            occurred_at=now + timedelta(hours=2),
        )
        async with context.runtime_factory.begin() as session:
            await EventIngestionService().ingest(session, reply)
        async with context.runtime_factory.begin() as session:
            after_reply = await gateway.persist_decision(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                process_definition_version=definition.version,
                decision=message_decision("after-reply"),
                policy=definition.action_policy(),
                communication_policy=policy,
                workflow_now=now + timedelta(hours=2),
            )
        assert len(after_reply) == 1
