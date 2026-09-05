"""PostgreSQL durability and fencing tests for model token/cost budgets."""

import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import ApplicationError
from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.actions.gateway import ActionGateway, ActionPersistenceConflict
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.budgets import (
    BreakerConflict,
    BreakerScope,
    CircuitBreakerService,
    ModelBudget,
    ModelUsage,
    ModelUsageService,
    estimate_cost_micros,
)
from tiramisu_agents.budgets.pricing import PRICE_TABLE_VERSION
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.communications import CommunicationPolicy
from tiramisu_agents.core.contracts.actions import ActionAttemptStatus
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.breakers import CircuitBreaker
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.models.usage import ModelUsageLedger
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.reviews.service import ReviewService
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities, AgentTurnCommand
from tiramisu_agents.testkit.deployment import TEST_DEPLOYMENT_RELEASE
from tiramisu_agents.testkit.scripted_agent import ScriptedAgent

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


@dataclass(frozen=True, slots=True)
class _TurnContext:
    runtime_factory: async_sessionmaker[AsyncSession]
    admin_factory: async_sessionmaker[AsyncSession]
    tenant_id: UUID
    process_id: UUID
    event_id: UUID
    command: AgentTurnCommand


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        await session.execute(
            delete(ModelUsageLedger).where(ModelUsageLedger.tenant_id == tenant_id)
        )
        await session.execute(delete(CircuitBreaker).where(CircuitBreaker.tenant_id == tenant_id))
        for model in (
            ActionAttempt,
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


@asynccontextmanager
async def _turn_context(
    make_agent: Callable[[UUID], ScriptedAgent],
) -> AsyncGenerator[tuple[_TurnContext, AgentTurnActivities, ScriptedAgent]]:
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
        source="budgets.test",
        source_event_id=f"source-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(
            ExternalReference(
                provider="budgets.test",
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
    try:
        async with admin_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"tenant-{tenant_id}",
                    name="Budget Test Tenant",
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
        agent = make_agent(event.event_id)
        activities = AgentTurnActivities(
            runtime_factory,
            registry,
            agent,
            compatibility=compatibility,
            deployment_release=TEST_DEPLOYMENT_RELEASE,
        )
        context = _TurnContext(
            runtime_factory=runtime_factory,
            admin_factory=admin_factory,
            tenant_id=tenant_id,
            process_id=ingested.process_instance_id,
            event_id=event.event_id,
            command=AgentTurnCommand(
                tenant_id=str(tenant_id),
                process_instance_id=str(ingested.process_instance_id),
                process_definition_id="enquiry_to_booking",
                process_definition_version=definition.version,
                turn_id=str(uuid4()),
                event_ids=(str(event.event_id),),
                workflow_now=datetime.now(UTC),
            ),
        )
        yield context, activities, agent
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()


def _waiting_decision(event_id: UUID) -> AgentDecision:
    return AgentDecision(
        based_on_event_ids=(event_id,),
        status=DecisionStatus.WAITING,
        wake_conditions=(EventWakeCondition(event_type="customer.email_received"),),
    )


@pytest.mark.asyncio
async def test_exhausted_budget_blocks_the_turn_before_any_model_call() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        activities,
        agent,
    ):
        definition = load_fictional_deployment().definition
        budget = ModelBudget.from_definition(definition)
        async with context.runtime_factory.begin() as session:
            await ModelUsageService().record(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                attempt_number=1,
                model="gpt-4o-mini",
                usage=ModelUsage(input_tokens=budget.max_input_tokens_per_process),
                cost_micros=0,
                price_table_version=PRICE_TABLE_VERSION,
            )
        with pytest.raises(ApplicationError) as raised:
            await activities.run_agent_turn(context.command)
        assert raised.value.non_retryable is True
        assert raised.value.type == "ModelBudgetExceeded"
        assert "input-token budget" in str(raised.value)
        assert agent.turn_inputs == []


@pytest.mark.asyncio
async def test_each_attempt_records_exact_usage_and_cost() -> None:
    usage = ModelUsage(input_tokens=1_000, output_tokens=250)
    async with _turn_context(
        lambda event_id: ScriptedAgent([_waiting_decision(event_id)], usages=[usage])
    ) as (context, activities, _agent):
        result = await activities.run_agent_turn(context.command)

        assert result.proposal_attempt_count == 1
        async with context.runtime_factory.begin() as session:
            await set_tenant_context(session, context.tenant_id)
            rows = (
                await session.scalars(
                    select(ModelUsageLedger).where(
                        ModelUsageLedger.tenant_id == context.tenant_id,
                        ModelUsageLedger.process_instance_id == context.process_id,
                    )
                )
            ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.agent_turn_id == UUID(context.command.turn_id)
        assert row.attempt_number == 1
        assert row.model == "gpt-4o-mini"
        assert row.input_tokens == 1_000
        assert row.output_tokens == 250
        assert row.cost_micros == estimate_cost_micros("gpt-4o-mini", usage)
        assert row.price_table_version == PRICE_TABLE_VERSION


@pytest.mark.asyncio
async def test_usage_recording_is_idempotent_for_retried_attempts() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        service = ModelUsageService()
        usage = ModelUsage(input_tokens=10, output_tokens=5)
        async with context.runtime_factory.begin() as session:
            first = await service.record(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=UUID(context.command.turn_id),
                attempt_number=1,
                model="gpt-4o-mini",
                usage=usage,
                cost_micros=estimate_cost_micros("gpt-4o-mini", usage),
                price_table_version=PRICE_TABLE_VERSION,
            )
            second = await service.record(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=UUID(context.command.turn_id),
                attempt_number=1,
                model="gpt-4o-mini",
                usage=usage,
                cost_micros=estimate_cost_micros("gpt-4o-mini", usage),
                price_table_version=PRICE_TABLE_VERSION,
            )
        assert first == second
        async with context.runtime_factory.begin() as session:
            await set_tenant_context(session, context.tenant_id)
            count = len(
                (
                    await session.scalars(
                        select(ModelUsageLedger).where(
                            ModelUsageLedger.tenant_id == context.tenant_id,
                            ModelUsageLedger.process_instance_id == context.process_id,
                        )
                    )
                ).all()
            )
        assert count == 1
        with pytest.raises(ValueError, match="lineage changed"):
            async with context.runtime_factory.begin() as session:
                await service.record(
                    session,
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=UUID(context.command.turn_id),
                    attempt_number=1,
                    model="gpt-4o-mini",
                    usage=ModelUsage(input_tokens=11, output_tokens=5),
                    cost_micros=0,
                    price_table_version=PRICE_TABLE_VERSION,
                )


@pytest.mark.asyncio
async def test_reexecuted_turn_records_new_spend() -> None:
    usages = [ModelUsage(input_tokens=100), ModelUsage(input_tokens=200)]
    async with _turn_context(
        lambda event_id: ScriptedAgent([_waiting_decision(event_id)] * 2, usages=usages)
    ) as (context, activities, _agent):
        # A lost Activity response can cause the same command to run again.
        await activities.run_agent_turn(context.command)
        await activities.run_agent_turn(context.command)
        async with context.runtime_factory.begin() as session:
            spent, cost = await ModelUsageService().spent(
                session, tenant_id=context.tenant_id, process_instance_id=context.process_id
            )
        assert spent == ModelUsage(input_tokens=300)
        assert cost == sum(estimate_cost_micros("gpt-4o-mini", usage) for usage in usages)


@pytest.mark.asyncio
async def test_correction_stops_when_first_call_exhausts_budget() -> None:
    budget = ModelBudget.from_definition(load_fictional_deployment().definition)
    async with _turn_context(
        lambda event_id: ScriptedAgent(
            [
                AgentDecision(based_on_event_ids=(), status=DecisionStatus.ACTIVE),
                _waiting_decision(event_id),
            ],
            usages=[ModelUsage(input_tokens=budget.max_input_tokens_per_process)],
        )
    ) as (context, activities, agent):
        with pytest.raises(ApplicationError, match="input-token budget"):
            await activities.run_agent_turn(context.command)
        assert len(agent.turn_inputs) == 1


@pytest.mark.asyncio
async def test_concurrent_breaker_trips_have_one_winner() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        results = await asyncio.gather(
            _trip(context, BreakerScope.MODEL_CALLS, ""),
            _trip(context, BreakerScope.MODEL_CALLS, ""),
            return_exceptions=True,
        )
        assert results.count(None) == 1
        assert sum(isinstance(result, BreakerConflict) for result in results) == 1


@pytest.mark.asyncio
async def test_spend_is_keyed_by_process_across_independent_runs() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        budget = ModelBudget.from_definition(load_fictional_deployment().definition)
        async with context.runtime_factory.begin() as session:
            await ModelUsageService().record(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                attempt_number=1,
                model="gpt-4o-mini",
                usage=ModelUsage(input_tokens=100, output_tokens=50),
                cost_micros=150,
                price_table_version=PRICE_TABLE_VERSION,
            )
        # A fresh service with no workflow memory sees identical spend: the
        # ledger, not Continue-As-New state, is the source of truth.
        async with context.runtime_factory.begin() as session:
            snapshot = await ModelUsageService().inspect(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                budget=budget,
            )
        assert snapshot.model_allowed_now is True
        assert snapshot.evaluated_spent == ModelUsage(input_tokens=100, output_tokens=50)
        assert snapshot.evaluated_cost_micros == 150


@pytest.mark.asyncio
async def test_unpriced_model_fails_closed_after_the_call() -> None:
    async with _turn_context(
        lambda event_id: ScriptedAgent([_waiting_decision(event_id)], model="unpriced-model")
    ) as (context, activities, agent):
        with pytest.raises(ApplicationError) as raised:
            await activities.run_agent_turn(context.command)
        assert raised.value.non_retryable is True
        assert raised.value.type == "UnknownModelPrice"
        assert len(agent.turn_inputs) == 1


def _message_decision(key: str) -> AgentDecision:
    return AgentDecision(
        based_on_event_ids=(),
        status=DecisionStatus.ACTIVE,
        actions=(
            ActionProposal(
                logical_action_key=key,
                action_type="send_message",
                parameters={"recipient": "customer@example.test", "body": key},
                rationale="Exercise breaker enforcement.",
            ),
        ),
    )


async def _trip(
    context: _TurnContext,
    scope: BreakerScope,
    target: str,
    reason: str = "Exercise breaker enforcement",
) -> None:
    async with context.runtime_factory.begin() as session:
        await CircuitBreakerService().trip(
            session,
            tenant_id=context.tenant_id,
            scope=scope,
            target=target,
            actor_id=uuid4(),
            reason=reason,
        )


@pytest.mark.asyncio
async def test_breaker_trip_reset_round_trip_with_conflicts() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        service = CircuitBreakerService()
        async with context.runtime_factory.begin() as session:
            assert (
                await service.latest(
                    session,
                    tenant_id=context.tenant_id,
                    scope=BreakerScope.MODEL_CALLS,
                )
                is None
            )
            with pytest.raises(BreakerConflict, match="already closed"):
                await service.reset(
                    session,
                    tenant_id=context.tenant_id,
                    scope=BreakerScope.MODEL_CALLS,
                    target="",
                    actor_id=uuid4(),
                    reason="Reset an untouched breaker",
                )
            tripped = await service.trip(
                session,
                tenant_id=context.tenant_id,
                scope=BreakerScope.MODEL_CALLS,
                target="",
                actor_id=uuid4(),
                reason="Exercise breaker enforcement",
            )
            assert tripped.tripped is True
            assert tripped.scope is BreakerScope.MODEL_CALLS
            with pytest.raises(BreakerConflict, match="already tripped"):
                await service.trip(
                    session,
                    tenant_id=context.tenant_id,
                    scope=BreakerScope.MODEL_CALLS,
                    target="",
                    actor_id=uuid4(),
                    reason="Exercise breaker enforcement",
                )
            closed = await service.reset(
                session,
                tenant_id=context.tenant_id,
                scope=BreakerScope.MODEL_CALLS,
                target="",
                actor_id=uuid4(),
                reason="Exercise breaker enforcement",
            )
            assert closed.tripped is False
            assert closed.transitioned_at > tripped.transitioned_at
            with pytest.raises(BreakerConflict, match="already closed"):
                await service.reset(
                    session,
                    tenant_id=context.tenant_id,
                    scope=BreakerScope.MODEL_CALLS,
                    target="",
                    actor_id=uuid4(),
                    reason="Exercise breaker enforcement",
                )
            latest = await service.latest(
                session,
                tenant_id=context.tenant_id,
                scope=BreakerScope.MODEL_CALLS,
            )
            assert latest is not None and latest.tripped is False


@pytest.mark.asyncio
async def test_tripped_model_breaker_blocks_the_turn_and_reset_unblocks() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        activities,
        agent,
    ):
        await _trip(context, BreakerScope.MODEL_CALLS, "")
        with pytest.raises(ApplicationError) as raised:
            await activities.run_agent_turn(context.command)
        assert raised.value.non_retryable is True
        assert raised.value.type == "ModelBudgetExceeded"
        assert "circuit breaker open (model_calls)" in str(raised.value)
        assert agent.turn_inputs == []

        async with context.runtime_factory.begin() as session:
            await CircuitBreakerService().reset(
                session,
                tenant_id=context.tenant_id,
                scope=BreakerScope.MODEL_CALLS,
                target="",
                actor_id=uuid4(),
                reason="Exercise breaker enforcement",
            )
        result = await activities.run_agent_turn(context.command)
        assert result.proposal_attempt_count == 1
        assert len(agent.turn_inputs) == 1


@pytest.mark.asyncio
async def test_capability_breaker_blocks_reservation() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        definition = load_fictional_deployment().definition
        await _trip(context, BreakerScope.CAPABILITY, "send_message")
        with pytest.raises(ActionPersistenceConflict, match="circuit breaker open"):
            async with context.runtime_factory.begin() as session:
                await ActionGateway().persist_decision(
                    session,
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=uuid4(),
                    process_definition_version=definition.version,
                    decision=_message_decision("blocked-by-capability"),
                    policy=definition.action_policy(),
                    communication_policy=CommunicationPolicy.from_definition(definition),
                    workflow_now=datetime.now(UTC),
                )


@pytest.mark.asyncio
async def test_outbound_breaker_blocks_reservation() -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        definition = load_fictional_deployment().definition
        await _trip(context, BreakerScope.OUTBOUND_MESSAGES, "")
        with pytest.raises(ActionPersistenceConflict, match="circuit breaker open"):
            async with context.runtime_factory.begin() as session:
                await ActionGateway().persist_decision(
                    session,
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    agent_turn_id=uuid4(),
                    process_definition_version=definition.version,
                    decision=_message_decision("blocked-by-outbound"),
                    policy=definition.action_policy(),
                    communication_policy=CommunicationPolicy.from_definition(definition),
                    workflow_now=datetime.now(UTC),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "target"),
    [
        (BreakerScope.CAPABILITY, "send_message"),
        (BreakerScope.OUTBOUND_MESSAGES, ""),
        (BreakerScope.ALL, ""),
    ],
)
async def test_breaker_blocks_execution_after_approval(scope: BreakerScope, target: str) -> None:
    async with _turn_context(lambda event_id: ScriptedAgent([_waiting_decision(event_id)])) as (
        context,
        _activities,
        _agent,
    ):
        definition = load_fictional_deployment().definition
        async with context.runtime_factory.begin() as session:
            persisted = await ActionGateway().persist_decision(
                session,
                tenant_id=context.tenant_id,
                process_instance_id=context.process_id,
                agent_turn_id=uuid4(),
                process_definition_version=definition.version,
                decision=_message_decision("approved-before-breaker"),
                policy=definition.action_policy(),
                communication_policy=CommunicationPolicy.from_definition(definition),
                workflow_now=datetime.now(UTC),
            )
        proposal = persisted[0]
        assert proposal.review_thread_id is not None
        async with context.runtime_factory.begin() as session:
            await ReviewService().apply(
                session,
                ReviewCommand(
                    tenant_id=context.tenant_id,
                    process_instance_id=context.process_id,
                    review_thread_id=proposal.review_thread_id,
                    action_request_id=proposal.action_request_id,
                    proposal_revision=proposal.revision,
                    actor_id=uuid4(),
                    command_type=ReviewCommandType.APPROVE,
                    expected_payload_hash=proposal.payload_hash,
                ),
            )
        await _trip(context, scope, target)

        adapter = StubActionAdapter()
        executor = ActionExecutor(
            context.runtime_factory,
            ActionAdapterRegistry({"send_message": adapter}),
            DeploymentCompatibility(
                client_pack_fingerprint="b" * 64,
                extension_manifest_hash="a" * 64,
                definition_fingerprints={
                    (definition.id, definition.version): definition.fingerprint()
                },
            ),
            TEST_DEPLOYMENT_RELEASE,
            ProcessDefinitionRegistry([definition]),
        )
        result = await executor.execute(
            tenant_id=context.tenant_id,
            process_instance_id=context.process_id,
            action_request_id=proposal.action_request_id,
            revision=proposal.revision,
        )
        assert result.status is ActionAttemptStatus.FAILED
        assert result.error is not None and "circuit breaker open" in result.error
        assert adapter.requests == []
