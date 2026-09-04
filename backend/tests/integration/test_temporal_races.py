"""Deterministic ordering tests for simultaneous mailbox conditions."""

import asyncio
import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.worker import Worker
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxActionResolution,
    MailboxControl,
    MailboxEvent,
    MailboxInput,
    MailboxReview,
    ProcessMailboxWorkflow,
    WakePlan,
)
from tiramisu_agents.testkit.temporal_environment import start_time_skipping_environment


def _decision_json(
    command: dict[str, Any],
    *,
    status: str = "waiting",
    actions: Sequence[dict[str, Any]] = (),
    wake_conditions: Sequence[dict[str, Any]] = (),
) -> str:
    return json.dumps(
        {
            "decision_id": str(uuid4()),
            "based_on_event_ids": command["event_ids"],
            "based_on_review_command_ids": command["review_command_ids"],
            "based_on_action_attempt_ids": command["action_attempt_ids"],
            "based_on_timer_ids": command["timer_ids"],
            "status": status,
            "actions": list(actions),
            "wake_conditions": list(wake_conditions),
            "memory_update": {},
        }
    )


@activity.defn(name="persist_process_state")
async def persist_process_state(_command: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "status": "active"}


@pytest.mark.asyncio
async def test_matching_event_wins_when_event_and_timer_are_ready_in_the_same_task() -> None:
    task_queue = f"event-timer-race-{uuid4()}"
    event = MailboxEvent(event_id=str(uuid4()), event_type="customer.replied")

    async with await start_time_skipping_environment() as environment:
        now = await environment.get_current_time()
        handle = await environment.client.start_workflow(
            ProcessMailboxWorkflow.run,
            MailboxInput(tenant_id="tenant-1", process_instance_id="process-1"),
            id=f"event-timer-race-{uuid4()}",
            task_queue=task_queue,
        )
        # With no worker polling yet, both signals are committed before the
        # first workflow task. The timer is already due when that task begins.
        await handle.signal(
            ProcessMailboxWorkflow.replace_wake_plan,
            WakePlan(
                event_types=(event.event_type,),
                timer_id="old-follow-up",
                timer_at=now,
            ),
        )
        await handle.signal(ProcessMailboxWorkflow.receive_event, event)

        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
        ):
            state = await handle.query(ProcessMailboxWorkflow.state)
            for _ in range(100):
                state = await handle.query(ProcessMailboxWorkflow.state)
                if state.wake_records:
                    break
                await asyncio.sleep(0.01)

            assert [record.reason for record in state.wake_records] == ["event"]
            assert state.wake_records[0].event_id == event.event_id
            assert state.buffered_events == ()
            assert state.wake_plan is None

            await handle.signal(ProcessMailboxWorkflow.close)
            assert (await handle.result()).closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_stop", ("takeover", "close"))
async def test_lifecycle_stop_during_model_activity_preempts_side_effects(
    lifecycle_stop: str,
) -> None:
    task_queue = f"{lifecycle_stop}-turn-race-{uuid4()}"
    turn_started = asyncio.Event()
    release_turn = asyncio.Event()
    activity_calls = {"persist_actions": 0, "persist_state": 0, "execute": 0}

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        turn_started.set()
        await release_turn.wait()
        return {
            "decision_json": _decision_json(
                command,
                actions=({"action_type": "send_message"},),
                wake_conditions=({"type": "human", "interaction": "operator"},),
            )
        }

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(_command: dict[str, Any]) -> dict[str, str]:
        activity_calls["persist_actions"] += 1
        return {
            "actions_json": json.dumps(
                [
                    {
                        "action_request_id": str(uuid4()),
                        "revision": 1,
                        "outcome": "allow",
                    }
                ]
            )
        }

    @activity.defn(name="persist_process_state")
    async def race_persist_process_state(_command: dict[str, Any]) -> dict[str, Any]:
        activity_calls["persist_state"] += 1
        return {"version": 1, "status": "active"}

    @activity.defn(name="execute_action")
    async def execute_action(_command: dict[str, Any]) -> dict[str, str]:
        activity_calls["execute"] += 1
        raise AssertionError("a takeover-preempted action must not execute")

    async with (
        await start_time_skipping_environment() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ProcessMailboxWorkflow],
            activities=[
                run_agent_turn,
                persist_agent_actions,
                race_persist_process_state,
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
            id=f"takeover-turn-race-{uuid4()}",
            task_queue=task_queue,
        )
        initial_event = MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created")
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        await asyncio.wait_for(turn_started.wait(), timeout=5)
        if lifecycle_stop == "takeover":
            await handle.signal(
                ProcessMailboxWorkflow.receive_control,
                MailboxControl(command_id=str(uuid4()), command_type="takeover"),
            )
        else:
            assert lifecycle_stop == "close"
            await handle.signal(ProcessMailboxWorkflow.close)
        release_turn.set()

        if lifecycle_stop == "takeover":
            state = await handle.query(ProcessMailboxWorkflow.state)
            for _ in range(100):
                state = await handle.query(ProcessMailboxWorkflow.state)
                if state.wake_plan == WakePlan(human_interactions=("operator",)):
                    break
                await asyncio.sleep(0.01)
        else:
            state = await asyncio.wait_for(handle.result(), timeout=5)

        assert activity_calls == {"persist_actions": 0, "persist_state": 0, "execute": 0}
        assert state.buffered_controls == ()
        assert len(state.turn_records) == 1
        assert state.turn_records[0].event_ids == (initial_event.event_id,)
        assert state.turn_records[0].error == "turn superseded by operator lifecycle control"
        if lifecycle_stop == "takeover":
            assert state.wake_plan == WakePlan(human_interactions=("operator",))
            await handle.signal(ProcessMailboxWorkflow.close)
            state = await handle.result()
        assert state.closed is True


@pytest.mark.asyncio
async def test_takeover_during_provider_activity_allows_only_the_already_started_effect() -> None:
    task_queue = f"takeover-provider-race-{uuid4()}"
    execute_started = asyncio.Event()
    release_execute = asyncio.Event()
    initial_event = MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created")
    action_request_ids = (str(uuid4()), str(uuid4()))
    action_attempt_id = str(uuid4())
    calls = {"model": 0, "execute": 0}

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        calls["model"] += 1
        return {
            "decision_json": _decision_json(
                command,
                actions=({"action_type": "send_message"}, {"action_type": "send_message"}),
            )
        }

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(_command: dict[str, Any]) -> dict[str, str]:
        return {
            "actions_json": json.dumps(
                [
                    {"action_request_id": action_id, "revision": 1, "outcome": "allow"}
                    for action_id in action_request_ids
                ]
            )
        }

    @activity.defn(name="execute_action")
    async def execute_action(command: dict[str, Any]) -> dict[str, str]:
        calls["execute"] += 1
        assert calls["execute"] == 1
        assert command["action_request_id"] == action_request_ids[0]
        execute_started.set()
        await release_execute.wait()
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": action_request_ids[0],
                    "attempt_id": action_attempt_id,
                    "status": "succeeded",
                    "idempotency_key": "b" * 64,
                    "provider_reference": "stub:message",
                    "result": {"sent": True},
                    "error": None,
                }
            )
        }

    async with (
        await start_time_skipping_environment() as environment,
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
            id=f"takeover-provider-race-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        await asyncio.wait_for(execute_started.wait(), timeout=5)
        await handle.signal(
            ProcessMailboxWorkflow.receive_control,
            MailboxControl(command_id=str(uuid4()), command_type="takeover"),
        )
        release_execute.set()

        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.wake_plan == WakePlan(human_interactions=("operator",)):
                break
            await asyncio.sleep(0.01)

        assert calls == {"model": 1, "execute": 1}
        assert len(state.turn_records) == 1
        assert state.turn_records[0].error == "turn superseded by operator lifecycle control"
        assert (
            json.loads(state.turn_records[0].execution_results_json or "[]")[0]["attempt_id"]
            == action_attempt_id
        )
        assert state.wake_plan == WakePlan(human_interactions=("operator",))
        await handle.signal(ProcessMailboxWorkflow.close)
        assert (await handle.result()).closed is True


@pytest.mark.asyncio
async def test_action_result_precedes_customer_event_when_both_arrive_during_execution() -> None:
    task_queue = f"result-event-race-{uuid4()}"
    action_request_id = str(uuid4())
    action_attempt_id = str(uuid4())
    execute_started = asyncio.Event()
    release_execute = asyncio.Event()
    initial_event = MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created")
    customer_event = MailboxEvent(event_id=str(uuid4()), event_type="customer.replied")

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        if command["event_ids"] == [initial_event.event_id]:
            return {
                "decision_json": _decision_json(
                    command,
                    actions=({"action_type": "send_message"},),
                    wake_conditions=({"type": "event", "event_type": customer_event.event_type},),
                )
            }
        if command["action_attempt_ids"]:
            return {
                "decision_json": _decision_json(
                    command,
                    wake_conditions=({"type": "event", "event_type": customer_event.event_type},),
                )
            }
        assert command["event_ids"] == [customer_event.event_id]
        return {"decision_json": _decision_json(command, status="completed")}

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(command: dict[str, Any]) -> dict[str, str]:
        actions = (
            [
                {
                    "action_request_id": action_request_id,
                    "revision": 1,
                    "outcome": "allow",
                }
            ]
            if command["event_ids"] == [initial_event.event_id]
            else []
        )
        return {"actions_json": json.dumps(actions)}

    @activity.defn(name="execute_action")
    async def execute_action(_command: dict[str, Any]) -> dict[str, str]:
        execute_started.set()
        await release_execute.wait()
        return {
            "result_json": json.dumps(
                {
                    "action_request_id": action_request_id,
                    "attempt_id": action_attempt_id,
                    "status": "succeeded",
                    "idempotency_key": "a" * 64,
                    "provider_reference": "stub:message",
                    "result": {"sent": True},
                    "error": None,
                }
            )
        }

    async with (
        await start_time_skipping_environment() as environment,
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
            id=f"result-event-race-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        await asyncio.wait_for(execute_started.wait(), timeout=5)
        await handle.signal(ProcessMailboxWorkflow.receive_event, customer_event)
        await handle.signal(ProcessMailboxWorkflow.receive_event, customer_event)
        during_execution = await handle.query(ProcessMailboxWorkflow.state)
        assert during_execution.buffered_events == (customer_event,)
        release_execute.set()

        result = await asyncio.wait_for(handle.result(), timeout=5)
        assert result.closed is True
        assert [record.event_ids for record in result.turn_records] == [
            (initial_event.event_id,),
            (),
            (customer_event.event_id,),
        ]
        assert result.turn_records[1].action_attempt_ids == (action_attempt_id,)
        assert len(result.execution_records) == 1


@pytest.mark.asyncio
async def test_review_commands_remain_fifo_when_more_arrive_during_review_turn() -> None:
    task_queue = f"review-turn-race-{uuid4()}"
    action_request_id = str(uuid4())
    first_comment_started = asyncio.Event()
    release_first_comment = asyncio.Event()
    initial_event = MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created")
    first_comment = MailboxReview(
        command_id=str(uuid4()),
        command_type="comment",
        review_thread_id=str(uuid4()),
        action_request_id=action_request_id,
        proposal_revision=1,
    )
    second_comment = MailboxReview(
        command_id=str(uuid4()),
        command_type="comment",
        review_thread_id=first_comment.review_thread_id,
        action_request_id=action_request_id,
        proposal_revision=1,
    )
    revision = MailboxReview(
        command_id=str(uuid4()),
        command_type="request_revision",
        review_thread_id=first_comment.review_thread_id,
        action_request_id=action_request_id,
        proposal_revision=1,
    )

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        if command["review_command_ids"] == [first_comment.command_id]:
            first_comment_started.set()
            await release_first_comment.wait()
        actions: tuple[dict[str, Any], ...] = (
            ({"action_type": "send_message"},) if command["event_ids"] else ()
        )
        return {
            "decision_json": _decision_json(
                command,
                actions=actions,
                wake_conditions=({"type": "human", "interaction": "approval"},),
            )
        }

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

    async with (
        await start_time_skipping_environment() as environment,
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
            id=f"review-turn-race-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(100):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.pending_action_request_ids:
                break
            await asyncio.sleep(0.01)
        assert state.pending_action_request_ids == (action_request_id,)

        await handle.signal(ProcessMailboxWorkflow.receive_review, first_comment)
        await asyncio.wait_for(first_comment_started.wait(), timeout=5)
        await handle.signal(ProcessMailboxWorkflow.receive_review, second_comment)
        await handle.signal(ProcessMailboxWorkflow.receive_review, revision)
        await handle.signal(ProcessMailboxWorkflow.receive_review, second_comment)
        release_first_comment.set()

        for _ in range(200):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.completed_turn_count == 4:
                break
            await asyncio.sleep(0.01)

        assert state.completed_turn_count == 4
        assert [record.review_command_ids for record in state.turn_records[1:]] == [
            (first_comment.command_id,),
            (second_comment.command_id,),
            (revision.command_id,),
        ]
        assert state.pending_action_request_ids == ()
        assert state.buffered_reviews == ()

        await handle.signal(ProcessMailboxWorkflow.close)
        assert (await handle.result()).closed is True


@pytest.mark.asyncio
async def test_continue_as_new_carries_review_resolution_and_control_buffers_once() -> None:
    task_queue = f"continuation-command-race-{uuid4()}"
    first_turn_started = asyncio.Event()
    release_first_turn = asyncio.Event()
    initial_event = MailboxEvent(event_id=str(uuid4()), event_type="enquiry.created")
    resolution = MailboxActionResolution(
        command_id=str(uuid4()),
        action_request_id=str(uuid4()),
        action_attempt_id=str(uuid4()),
        status="succeeded",
    )
    retry = MailboxControl(
        command_id=str(uuid4()),
        command_type="retry",
        timer_ids=("retry-source",),
    )
    review = MailboxReview(
        command_id=str(uuid4()),
        command_type="comment",
        review_thread_id=str(uuid4()),
        action_request_id=str(uuid4()),
        proposal_revision=1,
    )

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(command: dict[str, Any]) -> dict[str, str]:
        if command["event_ids"]:
            first_turn_started.set()
            await release_first_turn.wait()
        return {
            "decision_json": _decision_json(
                command,
                wake_conditions=({"type": "human", "interaction": "operator"},),
            )
        }

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(_command: dict[str, Any]) -> dict[str, str]:
        return {"actions_json": "[]"}

    async with (
        await start_time_skipping_environment() as environment,
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
                continue_as_new_after_turns=1,
            ),
            id=f"continuation-command-race-{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(ProcessMailboxWorkflow.receive_event, initial_event)
        await asyncio.wait_for(first_turn_started.wait(), timeout=5)
        await handle.signal(ProcessMailboxWorkflow.receive_control, retry)
        await handle.signal(ProcessMailboxWorkflow.receive_action_resolution, resolution)
        await handle.signal(ProcessMailboxWorkflow.receive_review, review)
        await handle.signal(ProcessMailboxWorkflow.receive_control, retry)
        await handle.signal(ProcessMailboxWorkflow.receive_action_resolution, resolution)
        await handle.signal(ProcessMailboxWorkflow.receive_review, review)
        release_first_turn.set()

        state = await handle.query(ProcessMailboxWorkflow.state)
        for _ in range(300):
            state = await handle.query(ProcessMailboxWorkflow.state)
            if state.completed_turn_count == 4 and state.continued_run_count >= 4:
                break
            await asyncio.sleep(0.01)

        assert state.completed_turn_count == 4
        assert state.continued_run_count >= 4
        assert [record.action_attempt_ids for record in state.turn_records] == [
            (),
            (),
            (resolution.action_attempt_id,),
            (),
        ]
        assert state.turn_records[1].review_command_ids == (review.command_id,)
        assert state.turn_records[3].timer_ids == retry.timer_ids
        assert state.buffered_reviews == ()
        assert state.buffered_action_resolutions == ()
        assert state.buffered_controls == ()

        await handle.signal(ProcessMailboxWorkflow.close)
        assert (await handle.result()).closed is True
