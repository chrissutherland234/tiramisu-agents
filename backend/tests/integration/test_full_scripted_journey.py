"""Full PostgreSQL + Temporal journey with worker restarts at provider boundaries."""

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tiramisu_agents.actions.execution import ActionExecutor
from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.adapters.stubs import StubBusinessState
from tiramisu_agents.agents.context import PostgresAgentContextLoader
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    HumanWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.processes import AgentTurnInput
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.core.reserved_events import OPERATOR_MANUAL_WAKE_EVENT_TYPE
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
from tiramisu_agents.events.ingestion import EventIngestionService
from tiramisu_agents.processes.control import (
    ProcessControlInput,
    ProcessControlService,
    ProcessControlType,
)
from tiramisu_agents.reviews.service import ReviewService
from tiramisu_agents.temporal.activities.action_execution import ActionExecutionActivities
from tiramisu_agents.temporal.activities.action_gateway import ActionGatewayActivities
from tiramisu_agents.temporal.activities.agent_turn import AgentTurnActivities
from tiramisu_agents.temporal.activities.process_state import ProcessStateActivities
from tiramisu_agents.temporal.dispatcher import DispatchStatus, TemporalOutboxDispatcher
from tiramisu_agents.temporal.workflows.mailbox import MailboxState, ProcessMailboxWorkflow
from tiramisu_agents.testkit import ScriptedAgent, make_test_deployment_release

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires migrated PostgreSQL and Temporal's test server",
)


def _decision_for(turn: AgentTurnInput) -> AgentDecision:
    if turn.events and turn.events[0].event_type == "enquiry.created":
        return _new_decision(
            turn,
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="find_initial_availability",
                    action_type="find_available_slots",
                    parameters={"days": 7},
                    rationale="Find authoritative appointment slots.",
                ),
            ),
        )
    if turn.events and turn.events[0].event_type == "customer.email_received":
        slot = turn.process.authoritative_facts["booking.available_slots"][0]
        return _new_decision(
            turn,
            status=DecisionStatus.WAITING,
            actions=(
                ActionProposal(
                    logical_action_key="propose_selected_booking",
                    action_type="propose_booking",
                    parameters={
                        "customer_id": turn.process.authoritative_facts["customer.email"],
                        "slot": slot,
                    },
                    rationale="Propose the exact slot selected by the customer.",
                ),
            ),
            wake_conditions=(HumanWakeCondition(interaction="approval"),),
        )
    if turn.events and turn.events[0].event_type == "payment.completed":
        return _new_decision(
            turn,
            status=DecisionStatus.ACTIVE,
            actions=(
                ActionProposal(
                    logical_action_key="create_paid_booking_calendar_event",
                    action_type="create_calendar_event",
                    parameters={
                        "booking_reference": turn.process.authoritative_facts["booking.reference"],
                        "starts_at": turn.process.authoritative_facts["booking.slot"],
                        "title": "Fictional customer appointment",
                    },
                    rationale="Create the calendar event after authoritative payment.",
                ),
            ),
        )
    if turn.events and turn.events[0].event_type == OPERATOR_MANUAL_WAKE_EVENT_TYPE:
        event = turn.events[0]
        assert event.payload["reason"] == "Customer says they paid cash; reconsider the case"
        assert event.payload["command_type"] == "wake"
        assert event.facts == ()
        assert turn.process.authoritative_facts["payment.status"] == "pending"
        return _new_decision(
            turn,
            status=DecisionStatus.WAITING,
            wake_conditions=(
                EventWakeCondition(event_type="payment.completed"),
                EventWakeCondition(event_type="payment.failed"),
            ),
        )

    action_type = turn.action_results[0].action_type
    if action_type == "find_available_slots":
        return _new_decision(
            turn,
            status=DecisionStatus.WAITING,
            actions=(
                ActionProposal(
                    logical_action_key="send_available_slots",
                    action_type="send_message",
                    parameters={
                        "recipient": turn.process.authoritative_facts["customer.email"],
                        "body": "We have 10:00 appointments available. Which date suits you?",
                    },
                    rationale="Send the authoritative availability to the customer.",
                ),
            ),
            wake_conditions=(HumanWakeCondition(interaction="approval"),),
        )
    if action_type == "send_message":
        return _new_decision(
            turn,
            status=DecisionStatus.WAITING,
            wake_conditions=(EventWakeCondition(event_type="customer.email_received"),),
        )
    if action_type == "propose_booking":
        result_facts = {fact.key: fact.value for fact in turn.action_results[0].facts}
        return _new_decision(
            turn,
            status=DecisionStatus.WAITING,
            actions=(
                ActionProposal(
                    logical_action_key="request_booking_payment",
                    action_type="request_payment",
                    parameters={
                        "booking_reference": result_facts["booking.reference"],
                        "amount_minor": 12_500,
                        "currency": "NZD",
                    },
                    rationale="Request payment for the confirmed booking.",
                ),
            ),
            wake_conditions=(HumanWakeCondition(interaction="approval"),),
        )
    if action_type == "request_payment":
        return _new_decision(
            turn,
            status=DecisionStatus.WAITING,
            wake_conditions=(
                EventWakeCondition(event_type="payment.completed"),
                EventWakeCondition(event_type="payment.failed"),
            ),
        )
    if action_type == "create_calendar_event":
        return _new_decision(turn, status=DecisionStatus.COMPLETED)
    raise AssertionError(f"unexpected scripted turn: {turn}")


def _new_decision(
    turn: AgentTurnInput,
    *,
    status: DecisionStatus,
    actions: tuple[ActionProposal, ...] = (),
    wake_conditions: tuple[EventWakeCondition | HumanWakeCondition, ...] = (),
) -> AgentDecision:
    return AgentDecision(
        based_on_event_ids=tuple(event.event_id for event in turn.events),
        based_on_review_command_ids=tuple(review.command_id for review in turn.reviews),
        based_on_action_attempt_ids=tuple(result.attempt_id for result in turn.action_results),
        based_on_timer_ids=turn.timer_ids,
        status=status,
        actions=actions,
        wake_conditions=wake_conditions,
    )


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
        ProcessIntervention,
        ProcessControlCommand,
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


async def _dispatch_all(dispatcher: TemporalOutboxDispatcher, tenant_id: UUID) -> None:
    for _ in range(20):
        result = await dispatcher.dispatch_one(tenant_id)
        if result.status is DispatchStatus.EMPTY:
            return
        assert result.status is DispatchStatus.PUBLISHED, result
    raise AssertionError("outbox did not drain")


async def _approve_open_review(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    process_instance_id: UUID,
    actor_id: UUID,
) -> None:
    async with session_factory.begin() as session:
        await set_tenant_context(session, tenant_id)
        row = (
            await session.execute(
                select(ReviewThread, ApprovalRequest)
                .join(ApprovalRequest, ApprovalRequest.id == ReviewThread.approval_request_id)
                .where(
                    ReviewThread.process_instance_id == process_instance_id,
                    ReviewThread.status == "open",
                    ApprovalRequest.status == "pending",
                )
                .order_by(ReviewThread.created_at.desc())
                .limit(1)
            )
        ).one()
        thread, approval = row
        await ReviewService().apply(
            session,
            ReviewCommand(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                review_thread_id=thread.id,
                action_request_id=approval.action_request_id,
                proposal_revision=approval.revision,
                command_type=ReviewCommandType.APPROVE,
                actor_id=actor_id,
                expected_payload_hash=approval.payload_hash,
            ),
        )


async def _wait_for_projection(
    session_factory: async_sessionmaker[AsyncSession],
    handle: Any,
    *,
    tenant_id: UUID,
    process_instance_id: UUID,
    status: str,
    human: tuple[str, ...] = (),
    events: tuple[str, ...] = (),
    minimum_version: int,
) -> MailboxState:
    for _ in range(300):
        async with session_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            process = await session.scalar(
                select(ProcessInstance).where(ProcessInstance.id == process_instance_id)
            )
            assert process is not None
            if process.status == status and process.state_version >= minimum_version:
                state = cast(
                    MailboxState,
                    await handle.query(ProcessMailboxWorkflow.state),
                )
                if state.turn_in_progress:
                    await asyncio.sleep(0.02)
                    continue
                wake = state.wake_plan
                temporal_human = wake.human_interactions if wake is not None else ()
                temporal_events = wake.event_types if wake is not None else ()
                db_human = tuple(
                    str(item["interaction"])
                    for item in process.current_wake_conditions
                    if item["type"] == "human"
                )
                db_events = tuple(
                    str(item["event_type"])
                    for item in process.current_wake_conditions
                    if item["type"] == "event"
                )
                if db_human == human == temporal_human and db_events == events == temporal_events:
                    return state
        await asyncio.sleep(0.02)
    raise AssertionError(f"process did not reach {status} with matching DB/Temporal wakes")


@pytest.mark.asyncio
async def test_full_scripted_journey_survives_worker_restarts() -> None:
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
    actor_id = uuid4()
    runner = ScriptedAgent([_decision_for] * 9)
    fixed_now = datetime(2026, 9, 1, 9, tzinfo=UTC)
    deployment = load_fictional_deployment(state=StubBusinessState(now=fixed_now))
    release = make_test_deployment_release(
        client_pack_fingerprint=deployment.fingerprint(),
        deployment_id="journey-deployment",
        build_id="journey-build",
    )
    task_queue = release.temporal_task_queue

    try:
        async with admin_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"tenant-{tenant_id}",
                    name="Journey Tenant",
                    deployment_id=release.deployment_id,
                )
            )
        enquiry = CanonicalEvent(
            tenant_id=tenant_id,
            event_type="enquiry.created",
            source="stub.website",
            source_event_id=f"enquiry-{uuid4()}",
            occurred_at=fixed_now,
            external_references=(
                ExternalReference(
                    provider="stub.website",
                    resource_type="enquiry",
                    external_id=f"enquiry-{uuid4()}",
                ),
            ),
            facts=(
                FactObservation(
                    key="customer.email",
                    kind=FactKind.AUTHORITATIVE,
                    value="scripted-customer@example.test",
                ),
            ),
        )
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                enquiry,
                bootstrap=deployment.trigger_rules(release)["enquiry.created"],
            )
        assert ingested.process_instance_id is not None
        process_id = ingested.process_instance_id

        async with await WorkflowEnvironment.start_time_skipping() as environment:

            @asynccontextmanager
            async def restarted_worker() -> AsyncGenerator[TemporalOutboxDispatcher]:
                fresh_deployment = load_fictional_deployment(state=StubBusinessState(now=fixed_now))
                authorized = frozenset({tenant_id})
                agent = AgentTurnActivities(
                    runtime_factory,
                    fresh_deployment.registry,
                    runner,
                    compatibility=fresh_deployment.compatibility,
                    deployment_release=release,
                    context_loader=PostgresAgentContextLoader(),
                    authorized_tenant_ids=authorized,
                )
                gateway = ActionGatewayActivities(
                    runtime_factory,
                    fresh_deployment.registry,
                    deployment_release=release,
                    authorized_tenant_ids=authorized,
                )
                state = ProcessStateActivities(
                    runtime_factory,
                    fresh_deployment.registry,
                    deployment_release=release,
                    authorized_tenant_ids=authorized,
                )
                execution = ActionExecutionActivities(
                    ActionExecutor(
                        runtime_factory,
                        ActionAdapterRegistry(fresh_deployment.bindings),
                        fresh_deployment.compatibility,
                        release,
                    ),
                    authorized_tenant_ids=authorized,
                )
                async with Worker(
                    environment.client,
                    task_queue=task_queue,
                    workflows=[ProcessMailboxWorkflow],
                    activities=[
                        agent.run_agent_turn,
                        gateway.persist_agent_actions,
                        state.persist_process_state,
                        state.record_process_intervention,
                        execution.execute_action,
                        execution.reconcile_action,
                    ],
                    max_cached_workflows=0,
                ):
                    yield TemporalOutboxDispatcher(
                        runtime_factory,
                        environment.client,
                        deployment_release=release,
                        authorized_tenant_ids=authorized,
                        orchestrate_agent_turns=True,
                    )

            workflow_id = f"tenant/{tenant_id}/process/{process_id}"
            handle = environment.client.get_workflow_handle_for(
                ProcessMailboxWorkflow.run,
                workflow_id,
            )

            async with restarted_worker() as dispatcher:
                await _dispatch_all(dispatcher, tenant_id)
                await _wait_for_projection(
                    runtime_factory,
                    handle,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    status="review",
                    human=("approval",),
                    minimum_version=2,
                )
                await _approve_open_review(
                    runtime_factory,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    actor_id=actor_id,
                )
                await _dispatch_all(dispatcher, tenant_id)
                await _wait_for_projection(
                    runtime_factory,
                    handle,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    status="waiting",
                    events=("customer.email_received",),
                    minimum_version=3,
                )

            reply = CanonicalEvent(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                event_type="customer.email_received",
                source="stub.messaging.v1",
                source_event_id=f"reply-{uuid4()}",
                occurred_at=fixed_now,
                facts=(
                    FactObservation(
                        key="customer.last_message",
                        kind=FactKind.CUSTOMER_CLAIM,
                        value="The second date at 10:00 please.",
                    ),
                ),
            )
            async with runtime_factory.begin() as session:
                await EventIngestionService().ingest(session, reply)

            async with restarted_worker() as dispatcher:
                await _dispatch_all(dispatcher, tenant_id)
                await _wait_for_projection(
                    runtime_factory,
                    handle,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    status="review",
                    human=("approval",),
                    minimum_version=4,
                )

            async with restarted_worker() as dispatcher:
                await _approve_open_review(
                    runtime_factory,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    actor_id=actor_id,
                )
                await _dispatch_all(dispatcher, tenant_id)
                await _wait_for_projection(
                    runtime_factory,
                    handle,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    status="review",
                    human=("approval",),
                    minimum_version=5,
                )

            async with restarted_worker() as dispatcher:
                await _approve_open_review(
                    runtime_factory,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    actor_id=actor_id,
                )
                await _dispatch_all(dispatcher, tenant_id)
                await _wait_for_projection(
                    runtime_factory,
                    handle,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    status="waiting",
                    events=("payment.completed", "payment.failed"),
                    minimum_version=6,
                )

            manual_reason = "Customer says they paid cash; reconsider the case"
            manual_wake = ProcessControlInput(
                command_id=uuid4(),
                tenant_id=tenant_id,
                process_instance_id=process_id,
                actor_id=actor_id,
                command_type=ProcessControlType.WAKE,
                reason=manual_reason,
            )
            async with runtime_factory.begin() as session:
                await ProcessControlService().apply_control(session, manual_wake)

            async with restarted_worker() as dispatcher:
                await _dispatch_all(dispatcher, tenant_id)
                manual_state = await _wait_for_projection(
                    runtime_factory,
                    handle,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    status="waiting",
                    events=("payment.completed", "payment.failed"),
                    minimum_version=7,
                )
                assert manual_state.wake_records[-1].reason == "operator_manual_wake"

            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_id)
                process = await session.get(ProcessInstance, process_id)
                manual_event = await session.scalar(
                    select(EventInbox).where(
                        EventInbox.process_instance_id == process_id,
                        EventInbox.event_type == OPERATOR_MANUAL_WAKE_EVENT_TYPE,
                    )
                )
                action_types_before_payment = set(
                    await session.scalars(
                        select(ActionRequest.action_type).where(
                            ActionRequest.process_instance_id == process_id
                        )
                    )
                )
            assert process is not None
            assert manual_event is not None
            assert manual_event.event_data["payload"] == {
                "reason": manual_reason,
                "actor_id": str(actor_id),
                "command_type": "wake",
            }
            assert process.authoritative_facts["payment.status"] == "pending"
            assert "calendar.status" not in process.authoritative_facts
            assert action_types_before_payment == {
                "find_available_slots",
                "send_message",
                "propose_booking",
                "request_payment",
            }

            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_id)
                process = await session.get(ProcessInstance, process_id)
                assert process is not None
                payment_reference = process.authoritative_facts["payment.reference"]
                booking_reference = process.authoritative_facts["booking.reference"]
            payment = CanonicalEvent(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                event_type="payment.completed",
                source="stub.payment.v1",
                source_event_id=f"completed-{payment_reference}",
                occurred_at=fixed_now,
                facts=(
                    FactObservation(
                        key="payment.reference",
                        kind=FactKind.AUTHORITATIVE,
                        value=payment_reference,
                    ),
                    FactObservation(
                        key="payment.status",
                        kind=FactKind.AUTHORITATIVE,
                        value="completed",
                    ),
                ),
                payload={"booking_reference": booking_reference},
            )
            async with runtime_factory.begin() as session:
                await EventIngestionService().ingest(session, payment)

            async with restarted_worker() as dispatcher:
                await _dispatch_all(dispatcher, tenant_id)
                result = await asyncio.wait_for(handle.result(), timeout=10)
                assert result.closed is True

            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_id)
                process = await session.get(ProcessInstance, process_id)
                attempts = (
                    await session.scalars(
                        select(ActionAttempt)
                        .where(ActionAttempt.process_instance_id == process_id)
                        .order_by(ActionAttempt.started_at)
                    )
                ).all()
            assert process is not None
            assert process.status == "completed"
            assert process.current_wake_conditions == []
            assert process.authoritative_facts["calendar.status"] == "created"
            assert len(attempts) == 5
            assert {attempt.status for attempt in attempts} == {"succeeded"}
            assert len(runner.turn_inputs) == 9
            manual_turn = next(
                turn
                for turn in runner.turn_inputs
                if turn.events and turn.events[0].event_type == OPERATOR_MANUAL_WAKE_EVENT_TYPE
            )
            assert manual_turn.events[0].payload["actor_id"] == str(actor_id)
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
