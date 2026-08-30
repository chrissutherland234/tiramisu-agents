"""A deterministic mailbox that owns one durable business process identity."""

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@dataclass(frozen=True)
class MailboxInput:
    tenant_id: str
    process_instance_id: str
    process_definition_id: str | None = None
    process_definition_version: str | None = None


@dataclass(frozen=True)
class MailboxEvent:
    event_id: str
    event_type: str


@dataclass(frozen=True)
class MailboxReview:
    command_id: str
    command_type: str
    review_thread_id: str
    action_request_id: str
    proposal_revision: int


@dataclass(frozen=True)
class WakePlan:
    """Wake conditions from the most recently accepted agent decision."""

    event_types: tuple[str, ...] = ()
    timer_id: str | None = None
    timer_at: datetime | None = None


@dataclass(frozen=True)
class WakeRecord:
    reason: str
    event_id: str | None
    event_type: str | None
    timer_id: str | None
    woke_at: datetime
    review_command_id: str | None = None
    review_command_type: str | None = None


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    event_ids: tuple[str, ...]
    review_command_ids: tuple[str, ...]
    timer_ids: tuple[str, ...]
    decision_json: str | None
    actions_json: str | None
    error: str | None


@dataclass(frozen=True)
class MailboxState:
    tenant_id: str
    process_instance_id: str
    buffered_events: tuple[MailboxEvent, ...]
    buffered_reviews: tuple[MailboxReview, ...]
    wake_records: tuple[WakeRecord, ...]
    turn_records: tuple[TurnRecord, ...]
    pending_action_request_ids: tuple[str, ...]
    turn_in_progress: bool
    wake_plan: WakePlan | None
    closed: bool


@workflow.defn
class ProcessMailboxWorkflow:
    """Durably buffers external events and sleeps until a declared condition matches."""

    def __init__(self) -> None:
        self._tenant_id = ""
        self._process_instance_id = ""
        self._buffered_events: list[MailboxEvent] = []
        self._seen_event_ids: set[str] = set()
        self._buffered_reviews: list[MailboxReview] = []
        self._seen_review_command_ids: set[str] = set()
        self._wake_records: list[WakeRecord] = []
        self._turn_records: list[TurnRecord] = []
        self._pending_action_request_ids: list[str] = []
        self._turn_in_progress = False
        self._process_definition_id: str | None = None
        self._process_definition_version: str | None = None
        self._wake_plan: WakePlan | None = None
        self._timer_due_at: datetime | None = None
        self._plan_revision = 0
        self._closed = False

    @workflow.run
    async def run(self, workflow_input: MailboxInput) -> MailboxState:
        self._tenant_id = workflow_input.tenant_id
        self._process_instance_id = workflow_input.process_instance_id
        self._process_definition_id = workflow_input.process_definition_id
        self._process_definition_version = workflow_input.process_definition_version

        while not self._closed:
            if not self._wake_records and self._wake_plan is None and self._buffered_events:
                event = self._buffered_events.pop(0)
                self._wake_records.append(
                    WakeRecord(
                        reason="process_started",
                        event_id=event.event_id,
                        event_type=event.event_type,
                        timer_id=None,
                        woke_at=workflow.now(),
                    )
                )
                await self._run_turn(event_ids=(event.event_id,))
                continue

            if self._buffered_reviews:
                review = self._buffered_reviews.pop(0)
                self._wake_records.append(
                    WakeRecord(
                        reason="review",
                        event_id=None,
                        event_type=None,
                        timer_id=None,
                        woke_at=workflow.now(),
                        review_command_id=review.command_id,
                        review_command_type=review.command_type,
                    )
                )
                self._clear_wake_plan()
                if review.command_type in {"comment", "request_revision"}:
                    await self._run_turn(review_command_ids=(review.command_id,))
                continue

            matching_index = self._matching_event_index()
            if matching_index is not None:
                event = self._buffered_events.pop(matching_index)
                self._wake_records.append(
                    WakeRecord(
                        reason="event",
                        event_id=event.event_id,
                        event_type=event.event_type,
                        timer_id=None,
                        woke_at=workflow.now(),
                    )
                )
                self._clear_wake_plan()
                await self._run_turn(event_ids=(event.event_id,))
                continue

            if self._timer_due_at is not None and self._timer_due_at <= workflow.now():
                timer_id = self._wake_plan.timer_id if self._wake_plan else None
                self._wake_records.append(
                    WakeRecord(
                        reason="timer",
                        event_id=None,
                        event_type=None,
                        timer_id=timer_id,
                        woke_at=workflow.now(),
                    )
                )
                self._clear_wake_plan()
                if timer_id is not None:
                    await self._run_turn(timer_ids=(timer_id,))
                continue

            revision = self._plan_revision
            timeout = self._timer_timeout()
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda awaited_revision=revision: (
                        self._closed
                        or self._plan_revision != awaited_revision
                        or bool(self._buffered_reviews)
                        or (
                            not self._wake_records
                            and self._wake_plan is None
                            and bool(self._buffered_events)
                        )
                        or self._matching_event_index() is not None
                    ),
                    timeout=timeout,
                    timeout_summary="waiting for process wake condition",
                )

        return self.state()

    @workflow.signal
    def receive_event(self, event: MailboxEvent) -> None:
        """Idempotently place an externally deduplicated event in the mailbox."""
        if event.event_id in self._seen_event_ids:
            return
        self._seen_event_ids.add(event.event_id)
        self._buffered_events.append(event)

    @workflow.signal
    def receive_review(self, review: MailboxReview) -> None:
        """Idempotently append one persisted human-review command."""
        if review.command_id in self._seen_review_command_ids:
            return
        self._seen_review_command_ids.add(review.command_id)
        self._buffered_reviews.append(review)

    @workflow.signal
    def replace_wake_plan(self, plan: WakePlan) -> None:
        """Atomically replace the previous turn's wake conditions."""
        self._wake_plan = plan
        self._timer_due_at = plan.timer_at
        self._plan_revision += 1

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def state(self) -> MailboxState:
        return MailboxState(
            tenant_id=self._tenant_id,
            process_instance_id=self._process_instance_id,
            buffered_events=tuple(self._buffered_events),
            buffered_reviews=tuple(self._buffered_reviews),
            wake_records=tuple(self._wake_records),
            turn_records=tuple(self._turn_records),
            pending_action_request_ids=tuple(self._pending_action_request_ids),
            turn_in_progress=self._turn_in_progress,
            wake_plan=self._wake_plan,
            closed=self._closed,
        )

    def _clear_wake_plan(self) -> None:
        self._wake_plan = None
        self._timer_due_at = None
        self._plan_revision += 1

    def _matching_event_index(self) -> int | None:
        if self._wake_plan is None:
            return None
        event_types = self._wake_plan.event_types
        return next(
            (
                index
                for index, event in enumerate(self._buffered_events)
                if event.event_type in event_types
            ),
            None,
        )

    def _timer_timeout(self) -> timedelta | None:
        if self._timer_due_at is None:
            return None
        return max(self._timer_due_at - workflow.now(), timedelta(0))

    async def _run_turn(
        self,
        *,
        event_ids: tuple[str, ...] = (),
        review_command_ids: tuple[str, ...] = (),
        timer_ids: tuple[str, ...] = (),
    ) -> None:
        if self._process_definition_id is None or self._process_definition_version is None:
            return
        turn_id = str(workflow.uuid4())
        workflow_now = workflow.now()
        self._turn_in_progress = True
        try:
            turn_result = cast(
                dict[str, Any],
                await workflow.execute_activity(
                    "run_agent_turn",
                    {
                        "tenant_id": self._tenant_id,
                        "process_instance_id": self._process_instance_id,
                        "process_definition_id": self._process_definition_id,
                        "process_definition_version": self._process_definition_version,
                        "turn_id": turn_id,
                        "event_ids": event_ids,
                        "workflow_now": workflow_now,
                        "review_command_ids": review_command_ids,
                        "timer_ids": timer_ids,
                    },
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                ),
            )
            decision_json = str(turn_result["decision_json"])
            action_result = cast(
                dict[str, Any],
                await workflow.execute_activity(
                    "persist_agent_actions",
                    {
                        "tenant_id": self._tenant_id,
                        "process_instance_id": self._process_instance_id,
                        "process_definition_id": self._process_definition_id,
                        "process_definition_version": self._process_definition_version,
                        "agent_turn_id": turn_id,
                        "event_ids": event_ids,
                        "workflow_now": workflow_now,
                        "decision_json": decision_json,
                        "review_command_ids": review_command_ids,
                        "timer_ids": timer_ids,
                    },
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                ),
            )
            actions_json = str(action_result["actions_json"])
            actions = cast(list[dict[str, Any]], json.loads(actions_json))
            self._pending_action_request_ids.extend(
                str(item["action_request_id"])
                for item in actions
                if str(item["action_request_id"]) not in self._pending_action_request_ids
            )
            self._turn_records.append(
                TurnRecord(
                    turn_id=turn_id,
                    event_ids=event_ids,
                    review_command_ids=review_command_ids,
                    timer_ids=timer_ids,
                    decision_json=decision_json,
                    actions_json=actions_json,
                    error=None,
                )
            )
            if not actions:
                self._apply_decision_wake_plan(decision_json)
        except ActivityError as error:
            self._turn_records.append(
                TurnRecord(
                    turn_id=turn_id,
                    event_ids=event_ids,
                    review_command_ids=review_command_ids,
                    timer_ids=timer_ids,
                    decision_json=None,
                    actions_json=None,
                    error=str(error),
                )
            )
        finally:
            self._turn_in_progress = False

    def _apply_decision_wake_plan(self, decision_json: str) -> None:
        decision = cast(dict[str, Any], json.loads(decision_json))
        wakes = cast(list[dict[str, Any]], decision.get("wake_conditions", []))
        event_types = tuple(str(item["event_type"]) for item in wakes if item["type"] == "event")
        timers = [
            (index, datetime.fromisoformat(str(item["at"])))
            for index, item in enumerate(wakes)
            if item["type"] == "timer"
        ]
        selected_timer = min(timers, key=lambda item: item[1]) if timers else None
        if decision.get("status") == "completed" and not wakes:
            self._closed = True
            return
        self._wake_plan = WakePlan(
            event_types=event_types,
            timer_id=(
                f"{decision['decision_id']}:timer:{selected_timer[0]}" if selected_timer else None
            ),
            timer_at=selected_timer[1] if selected_timer else None,
        )
        self._timer_due_at = self._wake_plan.timer_at
        self._plan_revision += 1
