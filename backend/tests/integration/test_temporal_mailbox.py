"""Deterministic mailbox tests using Temporal's time-skipping test server."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxActionResolution,
    MailboxContinuation,
    MailboxEvent,
    MailboxInput,
    MailboxReview,
    ProcessMailboxWorkflow,
    WakePlan,
)


@activity.defn(name="persist_process_state")
async def persist_process_state(_command: dict[str, Any]) -> dict[str, Any]:
    """Stand in for the database projection in workflow-only integration tests."""
    return {"version": 1, "status": "active"}


@pytest.mark.asyncio
async def test_mailbox_deduplicates_events_and_wakes_for_events_and_timers() -> None:
    task_queue = f"mailbox-test-{uuid4()}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
        ),
    ):
        now = await environment.get_current_time()
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(tenant_id="tenant-1", process_instance_id="process-1"),
            id=f"process-mailbox-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(
            ProcessMailboxWorkflow.replace_wake_plan,
            WakePlan(
                event_types=("email.received",),
                timer_id="follow-up",
                timer_at=now + timedelta(minutes=1),
            ),
        )
        payment = MailboxEvent(event_id="event-1", event_type="payment.received")
        email = MailboxEvent(event_id="event-2", event_type="email.received")
        await handle.signal(ProcessMailboxWorkflow.receive_event, payment)
        await handle.signal(ProcessMailboxWorkflow.receive_event, email)
        await handle.signal(ProcessMailboxWorkflow.receive_event, email)

        state = await handle.query(ProcessMailboxWorkflow.state)
        assert [wake.reason for wake in state.wake_records] == ["event"]
        assert state.buffered_events == (payment,)
        assert state.wake_plan is None

        await handle.signal(
            ProcessMailboxWorkflow.replace_wake_plan,
            WakePlan(
                timer_id="second-follow-up",
                timer_at=await environment.get_current_time() + timedelta(minutes=1),
            ),
        )
        await environment.sleep(timedelta(minutes=2))
        state = await handle.query(ProcessMailboxWorkflow.state)
        assert [wake.reason for wake in state.wake_records] == ["event", "timer"]
        assert state.wake_records[-1].timer_id == "second-follow-up"

        review = MailboxReview(
            command_id="review-command-1",
            command_type="request_revision",
            review_thread_id="review-thread-1",
            action_request_id="action-1",
            proposal_revision=1,
        )
        await handle.signal(ProcessMailboxWorkflow.receive_review, review)
        await handle.signal(ProcessMailboxWorkflow.receive_review, review)
        state = await handle.query(ProcessMailboxWorkflow.state)
        assert [wake.reason for wake in state.wake_records] == ["event", "timer", "review"]
        assert state.wake_records[-1].review_command_id == "review-command-1"
        assert state.buffered_reviews == ()

        await handle.signal(ProcessMailboxWorkflow.close)
        result = await handle.result()
        assert result.closed is True


@pytest.mark.asyncio
async def test_mailbox_recovers_buffered_events_and_wait_after_worker_restart() -> None:
    task_queue = f"restart-test-{uuid4()}"
    workflow_id = f"restarted-mailbox-{uuid4()}"
    payment = MailboxEvent(event_id="event-before-restart", event_type="payment.received")
    email = MailboxEvent(event_id="event-after-restart", event_type="email.received")

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            max_cached_workflows=0,
        ):
            handle = await environment.client.start_workflow(
                ProcessMailboxWorkflow.run,
                MailboxInput(tenant_id="tenant-1", process_instance_id="process-1"),
                id=workflow_id,
                task_queue=task_queue,
            )
            await handle.signal(
                ProcessMailboxWorkflow.replace_wake_plan,
                WakePlan(event_types=("email.received",)),
            )
            await handle.signal(ProcessMailboxWorkflow.receive_event, payment)
            state_before_restart = await handle.query(ProcessMailboxWorkflow.state)
            assert state_before_restart.buffered_events == (payment,)

        # There is deliberately no poller here: the open execution and its wait are durable.

        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            max_cached_workflows=0,
        ):
            handle = environment.client.get_workflow_handle_for(
                ProcessMailboxWorkflow.run, workflow_id
            )
            state_after_restart = await handle.query(ProcessMailboxWorkflow.state)
            assert state_after_restart.buffered_events == (payment,)
            assert state_after_restart.wake_plan == WakePlan(event_types=("email.received",))

            await handle.signal(ProcessMailboxWorkflow.receive_event, payment)
            await handle.signal(ProcessMailboxWorkflow.receive_event, email)
            recovered = await handle.query(ProcessMailboxWorkflow.state)
            assert recovered.buffered_events == (payment,)
            assert recovered.wake_plan is None
            assert recovered.wake_records[-1].event_id == email.event_id

            await handle.signal(ProcessMailboxWorkflow.close)
            assert (await handle.result()).closed is True


@pytest.mark.asyncio
async def test_persistence_retries_do_not_rerun_the_model_activity() -> None:
    task_queue = f"persistence-retry-test-{uuid4()}"
    model_calls = 0
    persistence_calls = 0

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        nonlocal model_calls
        model_calls += 1
        return {
            "decision_json": json.dumps(
                {
                    "decision_id": str(uuid4()),
                    "based_on_event_ids": command["event_ids"],
                    "based_on_review_command_ids": [],
                    "based_on_action_attempt_ids": [],
                    "based_on_timer_ids": [],
                    "status": "completed",
                    "actions": [],
                    "wake_conditions": [],
                    "memory_update": {},
                }
            )
        }

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(_command: dict[str, Any]) -> dict[str, str]:
        nonlocal persistence_calls
        persistence_calls += 1
        if persistence_calls < 3:
            raise RuntimeError("injected transient database failure")
        return {"actions_json": "[]"}

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[run_agent_turn, persist_agent_actions, persist_process_state],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id="tenant-1",
                process_instance_id="process-1",
                process_definition_id="example",
                process_definition_version="1",
            ),
            id=f"persistence-retry-mailbox-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(
            ProcessMailboxWorkflow.receive_event,
            MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created"),
        )

        result = await handle.result()
        assert result.closed is True
        assert result.completed_turn_count == 1
        assert result.turn_records[-1].error is None
        assert model_calls == 1
        assert persistence_calls == 3


@pytest.mark.asyncio
async def test_unknown_continuation_schema_fails_closed() -> None:
    task_queue = f"continuation-schema-test-{uuid4()}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id="tenant-1",
                process_instance_id="process-1",
                continuation=MailboxContinuation(schema_version=999),
            ),
            id=f"invalid-continuation-mailbox-{uuid4()}",
            task_queue=task_queue,
        )

        with pytest.raises(WorkflowFailureError) as raised:
            await handle.result()
        assert isinstance(raised.value.cause, ApplicationError)
        assert raised.value.cause.type == "UnsupportedMailboxContinuation"
        assert raised.value.cause.non_retryable is True


@pytest.mark.asyncio
async def test_mailbox_runs_event_timer_and_review_turns_single_flight() -> None:
    task_queue = f"orchestration-test-{uuid4()}"
    autonomous_action_id = str(uuid4())
    autonomous_attempt_id = str(uuid4())
    first_turn_started = asyncio.Event()
    release_first_turn = asyncio.Event()

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        if command["event_ids"]:
            first_turn_started.set()
            await release_first_turn.wait()
            workflow_now = datetime.fromisoformat(str(command["workflow_now"]))
            decision: dict[str, Any] = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": command["event_ids"],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": [],
                "status": "waiting",
                "actions": [
                    {
                        "logical_action_key": "lookup_1",
                        "action_type": "find_available_slots",
                        "parameters": {"days": 7},
                        "rationale": "Find availability.",
                    }
                ],
                "wake_conditions": [
                    {
                        "type": "timer",
                        "at": (workflow_now + timedelta(minutes=1)).isoformat(),
                    }
                ],
                "memory_update": {},
            }
        elif command["action_attempt_ids"]:
            workflow_now = datetime.fromisoformat(str(command["workflow_now"]))
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": command["action_attempt_ids"],
                "based_on_timer_ids": [],
                "status": "waiting",
                "actions": [],
                "wake_conditions": [
                    {
                        "type": "timer",
                        "at": (workflow_now + timedelta(minutes=1)).isoformat(),
                    }
                ],
                "memory_update": {},
            }
        elif command["timer_ids"]:
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": command["timer_ids"],
                "status": "waiting",
                "actions": [],
                "wake_conditions": [{"type": "human", "interaction": "review"}],
                "memory_update": {},
            }
        else:
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": command["review_command_ids"],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": [],
                "status": "completed",
                "actions": [],
                "wake_conditions": [],
                "memory_update": {},
            }
        return {"decision_json": json.dumps(decision)}

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(command: dict[str, Any]) -> dict[str, str]:
        actions = (
            [
                {
                    "action_request_id": autonomous_action_id,
                    "revision": 1,
                    "payload_hash": "a" * 64,
                    "outcome": "allow",
                    "status": "allowed",
                    "approval_request_id": None,
                    "review_thread_id": None,
                }
            ]
            if command["event_ids"]
            else []
        )
        return {"actions_json": json.dumps(actions)}

    @activity.defn(name="execute_action")
    async def execute_action(command: dict[str, Any]) -> dict[str, str]:
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": command["action_request_id"],
                    "attempt_id": autonomous_attempt_id,
                    "status": "unknown",
                    "idempotency_key": "b" * 64,
                    "provider_reference": None,
                    "result": None,
                    "error": "provider timed out",
                }
            )
        }

    @activity.defn(name="reconcile_action")
    async def reconcile_action(command: dict[str, Any]) -> dict[str, str]:
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": command["action_request_id"],
                    "attempt_id": autonomous_attempt_id,
                    "status": "succeeded",
                    "idempotency_key": "b" * 64,
                    "provider_reference": "stub:availability",
                    "result": {"slots": []},
                    "error": None,
                }
            )
        }

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[
                run_agent_turn,
                persist_agent_actions,
                persist_process_state,
                execute_action,
                reconcile_action,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id="tenant-1",
                process_instance_id="process-1",
                process_definition_id="example",
                process_definition_version="1",
            ),
            id=f"orchestrated-mailbox-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(
            ProcessMailboxWorkflow.receive_event,
            MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created"),
        )
        await first_turn_started.wait()
        queued_event = MailboxEvent(event_id=str(uuid4()), event_type="payment.completed")
        await handle.signal(ProcessMailboxWorkflow.receive_event, queued_event)
        state = await handle.query(ProcessMailboxWorkflow.state)
        assert state.turn_in_progress is True
        assert state.buffered_events == (queued_event,)
        release_first_turn.set()
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if len(state.turn_records) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(state.turn_records) == 2
        assert state.turn_records[0].event_ids
        assert state.turn_records[1].action_attempt_ids == (autonomous_attempt_id,)
        assert len(state.execution_records) == 2
        assert json.loads(state.execution_records[0].result_json or "{}")["status"] == "unknown"
        assert json.loads(state.execution_records[1].result_json or "{}")["status"] == "succeeded"
        assert state.pending_action_request_ids == ()

        await environment.sleep(timedelta(minutes=2))
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if len(state.turn_records) == 3:
                break
            await asyncio.sleep(0.01)
        assert len(state.turn_records) == 3
        assert state.turn_records[2].timer_ids
        assert state.buffered_events == (queued_event,)

        review_command_id = str(uuid4())
        await handle.signal(
            ProcessMailboxWorkflow.receive_review,
            MailboxReview(
                command_id=review_command_id,
                command_type="request_revision",
                review_thread_id=str(uuid4()),
                action_request_id=str(uuid4()),
                proposal_revision=1,
            ),
        )
        result = await handle.result()
        assert result.closed is True
        assert len(result.turn_records) == 4
        assert result.turn_records[3].review_command_ids == (review_command_id,)
        assert result.turn_in_progress is False


@pytest.mark.asyncio
async def test_mailbox_runs_result_turn_after_approved_action_executes() -> None:
    task_queue = f"approval-orchestration-test-{uuid4()}"
    action_request_id = str(uuid4())
    action_attempt_id = str(uuid4())

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        workflow_now = datetime.fromisoformat(str(command["workflow_now"]))
        if command["event_ids"]:
            decision: dict[str, Any] = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": command["event_ids"],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": [],
                "status": "waiting",
                "actions": [{"action_type": "send_message"}],
                "wake_conditions": [
                    {
                        "type": "timer",
                        "at": (workflow_now + timedelta(minutes=1)).isoformat(),
                    }
                ],
                "memory_update": {},
            }
        elif command["action_attempt_ids"]:
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": command["action_attempt_ids"],
                "based_on_timer_ids": [],
                "status": "waiting",
                "actions": [],
                "wake_conditions": [
                    {
                        "type": "timer",
                        "at": (workflow_now + timedelta(minutes=1)).isoformat(),
                    }
                ],
                "memory_update": {},
            }
        else:
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": command["timer_ids"],
                "status": "completed",
                "actions": [],
                "wake_conditions": [],
                "memory_update": {},
            }
        return {"decision_json": json.dumps(decision)}

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(command: dict[str, Any]) -> dict[str, str]:
        actions = (
            [
                {
                    "action_request_id": action_request_id,
                    "revision": 1,
                    "outcome": "require_approval",
                }
            ]
            if command["event_ids"]
            else []
        )
        return {"actions_json": json.dumps(actions)}

    @activity.defn(name="execute_action")
    async def execute_action(command: dict[str, Any]) -> dict[str, str]:
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": command["action_request_id"],
                    "attempt_id": action_attempt_id,
                    "status": "succeeded",
                    "idempotency_key": "c" * 64,
                    "provider_reference": "stub:message",
                    "result": {"sent": True},
                    "error": None,
                }
            )
        }

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[
                run_agent_turn,
                persist_agent_actions,
                persist_process_state,
                execute_action,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id="tenant-1",
                process_instance_id="process-1",
                process_definition_id="example",
                process_definition_version="1",
            ),
            id=f"approval-mailbox-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(
            ProcessMailboxWorkflow.receive_event,
            MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created"),
        )
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.pending_action_request_ids:
                break
            await asyncio.sleep(0.01)
        assert state.pending_action_request_ids == (action_request_id,)
        assert state.wake_plan is None
        assert state.execution_records == ()

        await handle.signal(
            ProcessMailboxWorkflow.receive_review,
            MailboxReview(
                command_id=str(uuid4()),
                command_type="approve",
                review_thread_id=str(uuid4()),
                action_request_id=action_request_id,
                proposal_revision=1,
            ),
        )
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.execution_records and state.wake_plan is not None:
                break
            await asyncio.sleep(0.01)
        assert state.pending_action_request_ids == ()
        assert len(state.execution_records) == 1
        assert state.wake_plan is not None
        assert len(state.turn_records) == 2
        assert state.turn_records[1].action_attempt_ids == (action_attempt_id,)

        resolution = MailboxActionResolution(
            command_id=str(uuid4()),
            action_request_id=action_request_id,
            action_attempt_id=action_attempt_id,
            status="succeeded",
        )
        await handle.signal(ProcessMailboxWorkflow.receive_action_resolution, resolution)
        await handle.signal(ProcessMailboxWorkflow.receive_action_resolution, resolution)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if len(state.turn_records) == 3:
                break
            await asyncio.sleep(0.01)
        assert len(state.turn_records) == 3
        assert state.turn_records[2].action_attempt_ids == (action_attempt_id,)
        assert state.buffered_action_resolutions == ()

        await environment.sleep(timedelta(minutes=2))
        result = await handle.result()
        assert result.closed is True
        assert len(result.turn_records) == 4


@pytest.mark.asyncio
async def test_autonomous_result_cannot_arm_a_timer_while_an_approval_is_pending() -> None:
    task_queue = f"mixed-action-test-{uuid4()}"
    autonomous_action_id = str(uuid4())
    approval_action_id = str(uuid4())
    attempt_id = str(uuid4())

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        workflow_now = datetime.fromisoformat(str(command["workflow_now"]))
        is_event_turn = bool(command["event_ids"])
        decision: dict[str, Any] = {
            "decision_id": str(uuid4()),
            "based_on_event_ids": command["event_ids"],
            "based_on_review_command_ids": [],
            "based_on_action_attempt_ids": command["action_attempt_ids"],
            "based_on_timer_ids": [],
            "status": "waiting",
            "actions": (
                [
                    {"action_type": "find_available_slots"},
                    {"action_type": "send_message"},
                ]
                if is_event_turn
                else []
            ),
            "wake_conditions": [
                {
                    "type": "timer",
                    "at": (workflow_now + timedelta(minutes=1)).isoformat(),
                }
            ],
            "memory_update": {},
        }
        return {"decision_json": json.dumps(decision)}

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(command: dict[str, Any]) -> dict[str, str]:
        actions = (
            [
                {
                    "action_request_id": autonomous_action_id,
                    "revision": 1,
                    "outcome": "allow",
                },
                {
                    "action_request_id": approval_action_id,
                    "revision": 1,
                    "outcome": "require_approval",
                },
            ]
            if command["event_ids"]
            else []
        )
        return {"actions_json": json.dumps(actions)}

    @activity.defn(name="execute_action")
    async def execute_action(command: dict[str, Any]) -> dict[str, str]:
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": command["action_request_id"],
                    "attempt_id": attempt_id,
                    "status": "succeeded",
                    "idempotency_key": "d" * 64,
                    "provider_reference": "stub:availability",
                    "result": {"slots": []},
                    "error": None,
                }
            )
        }

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[
                run_agent_turn,
                persist_agent_actions,
                persist_process_state,
                execute_action,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id="tenant-1",
                process_instance_id="process-1",
                process_definition_id="example",
                process_definition_version="1",
            ),
            id=f"mixed-action-mailbox-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(
            ProcessMailboxWorkflow.receive_event,
            MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created"),
        )

        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if len(state.turn_records) == 2 and not state.turn_in_progress:
                break
            await asyncio.sleep(0.01)

        assert state.pending_action_request_ids == (approval_action_id,)
        assert state.turn_records[1].action_attempt_ids == (attempt_id,)
        assert state.wake_plan is None

        await handle.signal(ProcessMailboxWorkflow.close)
        assert (await handle.result()).closed is True


@pytest.mark.asyncio
async def test_continue_as_new_preserves_mailbox_approval_deduplication_and_timer() -> None:
    task_queue = f"continue-as-new-test-{uuid4()}"
    action_request_id = str(uuid4())
    action_attempt_id = str(uuid4())
    initial_event = MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created")
    buffered_event = MailboxEvent(event_id=str(uuid4()), event_type="payment.completed")
    first_turn_started = asyncio.Event()
    release_first_turn = asyncio.Event()

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        workflow_now = datetime.fromisoformat(str(command["workflow_now"]))
        if command["event_ids"]:
            first_turn_started.set()
            await release_first_turn.wait()
            decision: dict[str, Any] = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": command["event_ids"],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": [],
                "status": "waiting",
                "actions": [{"action_type": "send_message"}],
                "wake_conditions": [{"type": "human", "interaction": "approval"}],
                "memory_update": {},
            }
        elif command["action_attempt_ids"]:
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": command["action_attempt_ids"],
                "based_on_timer_ids": [],
                "status": "waiting",
                "actions": [],
                "wake_conditions": [
                    {
                        "type": "timer",
                        "at": (workflow_now + timedelta(minutes=1)).isoformat(),
                    }
                ],
                "memory_update": {},
            }
        else:
            decision = {
                "decision_id": str(uuid4()),
                "based_on_event_ids": [],
                "based_on_review_command_ids": [],
                "based_on_action_attempt_ids": [],
                "based_on_timer_ids": command["timer_ids"],
                "status": "completed",
                "actions": [],
                "wake_conditions": [],
                "memory_update": {},
            }
        return {"decision_json": json.dumps(decision)}

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(command: dict[str, Any]) -> dict[str, str]:
        actions = (
            [
                {
                    "action_request_id": action_request_id,
                    "revision": 1,
                    "outcome": "require_approval",
                }
            ]
            if command["event_ids"]
            else []
        )
        return {"actions_json": json.dumps(actions)}

    @activity.defn(name="execute_action")
    async def execute_action(command: dict[str, Any]) -> dict[str, str]:
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": command["action_request_id"],
                    "attempt_id": action_attempt_id,
                    "status": "succeeded",
                    "idempotency_key": "e" * 64,
                    "provider_reference": "stub:message",
                    "result": {"sent": True},
                    "error": None,
                }
            )
        }

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[
                run_agent_turn,
                persist_agent_actions,
                persist_process_state,
                execute_action,
            ],
        ),
    ):
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(
                tenant_id="tenant-1",
                process_instance_id="process-1",
                process_definition_id="example",
                process_definition_version="1",
                continue_as_new_after_turns=1,
            ),
            id=f"continued-mailbox-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        await first_turn_started.wait()
        await handle.signal(ProcessMailboxWorkflow.receive_event, buffered_event)
        release_first_turn.set()

        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.continued_run_count == 1:
                break
            await asyncio.sleep(0.01)
        assert state.continued_run_count == 1
        assert state.completed_turn_count == 1
        assert state.pending_action_request_ids == (action_request_id,)
        assert state.buffered_events == (buffered_event,)
        assert state.wake_plan is None

        # A delivery retried after the run boundary remains deduplicated.
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        assert (await handle.query(ProcessMailboxWorkflow.state)).buffered_events == (
            buffered_event,
        )

        await handle.signal(
            ProcessMailboxWorkflow.receive_review,
            MailboxReview(
                command_id=str(uuid4()),
                command_type="approve",
                review_thread_id=str(uuid4()),
                action_request_id=action_request_id,
                proposal_revision=1,
            ),
        )
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.continued_run_count == 2 and state.wake_plan is not None:
                break
            await asyncio.sleep(0.01)
        assert state.continued_run_count == 2
        assert state.completed_turn_count == 2
        assert state.pending_action_request_ids == ()
        assert state.wake_plan is not None
        assert state.wake_plan.timer_at is not None
        assert len(state.execution_records) == 1

        await environment.sleep(timedelta(minutes=2))
        result = await handle.result()
        assert result.closed is True
        assert result.completed_turn_count == 3
        assert result.continued_run_count == 2
        assert result.buffered_events == (buffered_event,)
