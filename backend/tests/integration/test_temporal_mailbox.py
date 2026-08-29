"""Deterministic mailbox tests using Temporal's time-skipping test server."""

from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxEvent,
    MailboxInput,
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

        await handle.signal(ProcessMailboxWorkflow.close)
        result = await handle.result()
        assert result.closed is True
