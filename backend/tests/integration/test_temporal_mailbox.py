"""Deterministic mailbox tests using Temporal's time-skipping test server."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxEvent,
    MailboxInput,
    MailboxReview,
    ProcessMailboxWorkflow,
    WakePlan,
)


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
async def test_mailbox_runs_event_timer_and_review_turns_single_flight() -> None:
    task_queue = f"orchestration-test-{uuid4()}"
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
                "based_on_timer_ids": [],
                "status": "completed",
                "actions": [],
                "wake_conditions": [],
                "memory_update": {},
            }
        return {"decision_json": json.dumps(decision)}

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(_: dict[str, Any]) -> dict[str, str]:
        return {"actions_json": "[]"}

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[run_agent_turn, persist_agent_actions],
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
            if len(state.turn_records) == 1:
                break
            await asyncio.sleep(0.01)
        assert len(state.turn_records) == 1
        assert state.turn_records[0].event_ids

        await environment.sleep(timedelta(minutes=2))
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if len(state.turn_records) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(state.turn_records) == 2
        assert state.turn_records[1].timer_ids
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
        assert len(result.turn_records) == 3
        assert result.turn_records[2].review_command_ids == (review_command_id,)
        assert result.turn_in_progress is False
