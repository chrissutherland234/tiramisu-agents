"""A deterministic mailbox that owns one durable business process identity."""

import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError


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
class MailboxActionResolution:
    command_id: str
    action_request_id: str
    action_attempt_id: str
    status: str


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
    action_attempt_ids: tuple[str, ...]
    timer_ids: tuple[str, ...]
    decision_json: str | None
    actions_json: str | None
    execution_results_json: str | None
    error: str | None


@dataclass(frozen=True)
class ExecutionRecord:
    action_request_id: str
    revision: int
    result_json: str | None
    error: str | None


@dataclass(frozen=True)
class MailboxContinuation:
    """Versioned active state carried across a Continue-As-New boundary."""

    schema_version: int = 1
    started: bool = False
    buffered_events: tuple[dict[str, Any], ...] = ()
    seen_event_ids: tuple[str, ...] = ()
    buffered_reviews: tuple[dict[str, Any], ...] = ()
    seen_review_command_ids: tuple[str, ...] = ()
    buffered_action_resolutions: tuple[dict[str, Any], ...] = ()
    seen_action_resolution_ids: tuple[str, ...] = ()
    recent_wake_records: tuple[dict[str, Any], ...] = ()
    recent_turn_records: tuple[dict[str, Any], ...] = ()
    recent_execution_records: tuple[dict[str, Any], ...] = ()
    pending_action_request_ids: tuple[str, ...] = ()
    wake_plan: dict[str, Any] | None = None
    completed_turn_count: int = 0
    continued_run_count: int = 0


@dataclass(frozen=True)
class MailboxInput:
    tenant_id: str
    process_instance_id: str
    process_definition_id: str | None = None
    process_definition_version: str | None = None
    continue_as_new_after_turns: int = 100
    continuation: MailboxContinuation = field(default_factory=MailboxContinuation)


@dataclass(frozen=True)
class MailboxState:
    tenant_id: str
    process_instance_id: str
    buffered_events: tuple[MailboxEvent, ...]
    buffered_reviews: tuple[MailboxReview, ...]
    buffered_action_resolutions: tuple[MailboxActionResolution, ...]
    wake_records: tuple[WakeRecord, ...]
    turn_records: tuple[TurnRecord, ...]
    execution_records: tuple[ExecutionRecord, ...]
    pending_action_request_ids: tuple[str, ...]
    turn_in_progress: bool
    wake_plan: WakePlan | None
    closed: bool
    completed_turn_count: int
    continued_run_count: int


@workflow.defn
class ProcessMailboxWorkflow:
    """Durably buffers external events and sleeps until a declared condition matches."""

    _CONTINUATION_SCHEMA_VERSION = 1
    _RECENT_RECORD_LIMIT = 50

    def __init__(self) -> None:
        self._tenant_id = ""
        self._process_instance_id = ""
        self._buffered_events: list[MailboxEvent] = []
        self._seen_event_ids: set[str] = set()
        self._buffered_reviews: list[MailboxReview] = []
        self._seen_review_command_ids: set[str] = set()
        self._buffered_action_resolutions: list[MailboxActionResolution] = []
        self._seen_action_resolution_ids: set[str] = set()
        self._wake_records: list[WakeRecord] = []
        self._turn_records: list[TurnRecord] = []
        self._execution_records: list[ExecutionRecord] = []
        self._pending_action_request_ids: list[str] = []
        self._turn_in_progress = False
        self._process_definition_id: str | None = None
        self._process_definition_version: str | None = None
        self._wake_plan: WakePlan | None = None
        self._timer_due_at: datetime | None = None
        self._plan_revision = 0
        self._closed = False
        self._started = False
        self._continue_as_new_after_turns = 100
        self._turns_since_continue = 0
        self._completed_turn_count = 0
        self._continued_run_count = 0

    @workflow.run
    async def run(self, workflow_input: MailboxInput) -> MailboxState:
        self._tenant_id = workflow_input.tenant_id
        self._process_instance_id = workflow_input.process_instance_id
        self._process_definition_id = workflow_input.process_definition_id
        self._process_definition_version = workflow_input.process_definition_version
        self._continue_as_new_after_turns = max(1, workflow_input.continue_as_new_after_turns)
        self._restore_continuation(workflow_input.continuation)

        while not self._closed:
            if self._should_continue_as_new():
                workflow.continue_as_new(self._continuation_input())

            if not self._started and self._wake_plan is None and self._buffered_events:
                event = self._buffered_events.pop(0)
                self._started = True
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
                self._started = True
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
                if review.command_type == "approve":
                    execution = await self._execute_pending_action(
                        review.action_request_id, review.proposal_revision
                    )
                    if execution is not None:
                        await self._run_turn(action_attempt_ids=(str(execution["attempt_id"]),))
                elif review.command_type in {"reject", "request_revision"}:
                    self._remove_pending_action(review.action_request_id)
                    await self._run_turn(review_command_ids=(review.command_id,))
                elif review.command_type == "comment":
                    await self._run_turn(review_command_ids=(review.command_id,))
                continue

            if self._buffered_action_resolutions:
                resolution = self._buffered_action_resolutions.pop(0)
                self._started = True
                self._remove_pending_action(resolution.action_request_id)
                self._clear_wake_plan()
                await self._run_turn(action_attempt_ids=(resolution.action_attempt_id,))
                continue

            matching_index = self._matching_event_index()
            if matching_index is not None:
                event = self._buffered_events.pop(matching_index)
                self._started = True
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
                self._started = True
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
                        or bool(self._buffered_action_resolutions)
                        or (
                            not self._started
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
    def receive_action_resolution(self, resolution: MailboxActionResolution) -> None:
        """Idempotently deliver one persisted operator reconciliation decision."""
        if resolution.command_id in self._seen_action_resolution_ids:
            return
        self._seen_action_resolution_ids.add(resolution.command_id)
        self._buffered_action_resolutions.append(resolution)

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
            buffered_action_resolutions=tuple(self._buffered_action_resolutions),
            wake_records=tuple(self._wake_records),
            turn_records=tuple(self._turn_records),
            execution_records=tuple(self._execution_records),
            pending_action_request_ids=tuple(self._pending_action_request_ids),
            turn_in_progress=self._turn_in_progress,
            wake_plan=self._wake_plan,
            closed=self._closed,
            completed_turn_count=self._completed_turn_count,
            continued_run_count=self._continued_run_count,
        )

    def _should_continue_as_new(self) -> bool:
        if self._turn_in_progress:
            return False
        return (
            self._turns_since_continue >= self._continue_as_new_after_turns
            or workflow.info().is_continue_as_new_suggested()
        )

    def _continuation_input(self) -> MailboxInput:
        continuation = MailboxContinuation(
            schema_version=self._CONTINUATION_SCHEMA_VERSION,
            started=self._started,
            buffered_events=tuple(
                {"event_id": event.event_id, "event_type": event.event_type}
                for event in self._buffered_events
            ),
            seen_event_ids=tuple(sorted(self._seen_event_ids)),
            buffered_reviews=tuple(
                {
                    "command_id": review.command_id,
                    "command_type": review.command_type,
                    "review_thread_id": review.review_thread_id,
                    "action_request_id": review.action_request_id,
                    "proposal_revision": review.proposal_revision,
                }
                for review in self._buffered_reviews
            ),
            seen_review_command_ids=tuple(sorted(self._seen_review_command_ids)),
            buffered_action_resolutions=tuple(
                {
                    "command_id": resolution.command_id,
                    "action_request_id": resolution.action_request_id,
                    "action_attempt_id": resolution.action_attempt_id,
                    "status": resolution.status,
                }
                for resolution in self._buffered_action_resolutions
            ),
            seen_action_resolution_ids=tuple(sorted(self._seen_action_resolution_ids)),
            recent_wake_records=tuple(
                {
                    "reason": record.reason,
                    "event_id": record.event_id,
                    "event_type": record.event_type,
                    "timer_id": record.timer_id,
                    "woke_at": record.woke_at.isoformat(),
                    "review_command_id": record.review_command_id,
                    "review_command_type": record.review_command_type,
                }
                for record in self._wake_records[-self._RECENT_RECORD_LIMIT :]
            ),
            recent_turn_records=tuple(
                {
                    "turn_id": record.turn_id,
                    "event_ids": record.event_ids,
                    "review_command_ids": record.review_command_ids,
                    "action_attempt_ids": record.action_attempt_ids,
                    "timer_ids": record.timer_ids,
                    "decision_json": record.decision_json,
                    "actions_json": record.actions_json,
                    "execution_results_json": record.execution_results_json,
                    "error": record.error,
                }
                for record in self._turn_records[-self._RECENT_RECORD_LIMIT :]
            ),
            recent_execution_records=tuple(
                {
                    "action_request_id": record.action_request_id,
                    "revision": record.revision,
                    "result_json": record.result_json,
                    "error": record.error,
                }
                for record in self._execution_records[-self._RECENT_RECORD_LIMIT :]
            ),
            pending_action_request_ids=tuple(self._pending_action_request_ids),
            wake_plan=(
                {
                    "event_types": self._wake_plan.event_types,
                    "timer_id": self._wake_plan.timer_id,
                    "timer_at": (
                        self._wake_plan.timer_at.isoformat()
                        if self._wake_plan.timer_at is not None
                        else None
                    ),
                }
                if self._wake_plan is not None
                else None
            ),
            completed_turn_count=self._completed_turn_count,
            continued_run_count=self._continued_run_count + 1,
        )
        return MailboxInput(
            tenant_id=self._tenant_id,
            process_instance_id=self._process_instance_id,
            process_definition_id=self._process_definition_id,
            process_definition_version=self._process_definition_version,
            continue_as_new_after_turns=self._continue_as_new_after_turns,
            continuation=continuation,
        )

    def _restore_continuation(self, continuation: MailboxContinuation) -> None:
        if continuation.schema_version != self._CONTINUATION_SCHEMA_VERSION:
            raise ApplicationError(
                f"unsupported mailbox continuation schema {continuation.schema_version}",
                type="UnsupportedMailboxContinuation",
                non_retryable=True,
            )
        prestart_events = tuple(self._buffered_events)
        prestart_reviews = tuple(self._buffered_reviews)
        prestart_action_resolutions = tuple(self._buffered_action_resolutions)
        prestart_wake_plan = self._wake_plan
        self._started = continuation.started
        self._buffered_events = [
            MailboxEvent(
                event_id=str(document["event_id"]),
                event_type=str(document["event_type"]),
            )
            for document in continuation.buffered_events
        ]
        self._seen_event_ids = set(continuation.seen_event_ids)
        self._buffered_reviews = [
            MailboxReview(
                command_id=str(document["command_id"]),
                command_type=str(document["command_type"]),
                review_thread_id=str(document["review_thread_id"]),
                action_request_id=str(document["action_request_id"]),
                proposal_revision=int(document["proposal_revision"]),
            )
            for document in continuation.buffered_reviews
        ]
        self._seen_review_command_ids = set(continuation.seen_review_command_ids)
        self._buffered_action_resolutions = [
            MailboxActionResolution(
                command_id=str(document["command_id"]),
                action_request_id=str(document["action_request_id"]),
                action_attempt_id=str(document["action_attempt_id"]),
                status=str(document["status"]),
            )
            for document in continuation.buffered_action_resolutions
        ]
        self._seen_action_resolution_ids = set(continuation.seen_action_resolution_ids)
        self._wake_records = [
            WakeRecord(
                reason=str(document["reason"]),
                event_id=cast(str | None, document["event_id"]),
                event_type=cast(str | None, document["event_type"]),
                timer_id=cast(str | None, document["timer_id"]),
                woke_at=datetime.fromisoformat(str(document["woke_at"])),
                review_command_id=cast(str | None, document["review_command_id"]),
                review_command_type=cast(str | None, document["review_command_type"]),
            )
            for document in continuation.recent_wake_records
        ]
        self._turn_records = [
            TurnRecord(
                turn_id=str(document["turn_id"]),
                event_ids=tuple(str(value) for value in document["event_ids"]),
                review_command_ids=tuple(str(value) for value in document["review_command_ids"]),
                action_attempt_ids=tuple(str(value) for value in document["action_attempt_ids"]),
                timer_ids=tuple(str(value) for value in document["timer_ids"]),
                decision_json=cast(str | None, document["decision_json"]),
                actions_json=cast(str | None, document["actions_json"]),
                execution_results_json=cast(str | None, document["execution_results_json"]),
                error=cast(str | None, document["error"]),
            )
            for document in continuation.recent_turn_records
        ]
        self._execution_records = [
            ExecutionRecord(
                action_request_id=str(document["action_request_id"]),
                revision=int(document["revision"]),
                result_json=cast(str | None, document["result_json"]),
                error=cast(str | None, document["error"]),
            )
            for document in continuation.recent_execution_records
        ]
        self._pending_action_request_ids = list(continuation.pending_action_request_ids)
        wake_plan = continuation.wake_plan
        self._wake_plan = (
            WakePlan(
                event_types=tuple(str(value) for value in wake_plan["event_types"]),
                timer_id=cast(str | None, wake_plan["timer_id"]),
                timer_at=(
                    datetime.fromisoformat(str(wake_plan["timer_at"]))
                    if wake_plan["timer_at"] is not None
                    else None
                ),
            )
            if wake_plan is not None
            else None
        )
        self._timer_due_at = self._wake_plan.timer_at if self._wake_plan else None
        self._completed_turn_count = continuation.completed_turn_count
        self._continued_run_count = continuation.continued_run_count

        # Signal-With-Start handlers may run before the workflow method. Merge those
        # deliveries after the carried snapshot rather than erasing them on startup.
        for event in prestart_events:
            self.receive_event(event)
        for review in prestart_reviews:
            self.receive_review(review)
        for resolution in prestart_action_resolutions:
            self.receive_action_resolution(resolution)
        if prestart_wake_plan is not None:
            self._wake_plan = prestart_wake_plan
            self._timer_due_at = prestart_wake_plan.timer_at

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
        action_attempt_ids: tuple[str, ...] = (),
        timer_ids: tuple[str, ...] = (),
        chain_depth: int = 0,
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
                        "action_attempt_ids": action_attempt_ids,
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
                        "action_attempt_ids": action_attempt_ids,
                        "timer_ids": timer_ids,
                    },
                    start_to_close_timeout=timedelta(minutes=1),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                ),
            )
            actions_json = str(action_result["actions_json"])
            await workflow.execute_activity(
                "persist_process_state",
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
                    "action_attempt_ids": action_attempt_ids,
                    "timer_ids": timer_ids,
                },
                start_to_close_timeout=timedelta(minutes=1),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            actions = cast(list[dict[str, Any]], json.loads(actions_json))
            execution_results: list[dict[str, Any]] = []
            requires_approval = False
            for item in actions:
                action_request_id = str(item["action_request_id"])
                if item["outcome"] == "allow":
                    execution = await self._execute_pending_action(
                        action_request_id, int(item["revision"])
                    )
                    if execution is not None:
                        execution_results.append(execution)
                elif item["outcome"] == "require_approval":
                    self._add_pending_action(action_request_id)
                    requires_approval = True
            result_attempt_ids = tuple(str(result["attempt_id"]) for result in execution_results)
            chain_limit_reached = bool(result_attempt_ids) and chain_depth >= 5
            self._turn_records.append(
                TurnRecord(
                    turn_id=turn_id,
                    event_ids=event_ids,
                    review_command_ids=review_command_ids,
                    action_attempt_ids=action_attempt_ids,
                    timer_ids=timer_ids,
                    decision_json=decision_json,
                    actions_json=actions_json,
                    execution_results_json=json.dumps(
                        execution_results, sort_keys=True, separators=(",", ":")
                    ),
                    error=(
                        "automatic action-result chain limit reached"
                        if chain_limit_reached
                        else None
                    ),
                )
            )
            if result_attempt_ids:
                if not chain_limit_reached:
                    await self._run_turn(
                        action_attempt_ids=result_attempt_ids,
                        chain_depth=chain_depth + 1,
                    )
                else:
                    self._clear_wake_plan()
            elif requires_approval or self._pending_action_request_ids:
                self._clear_wake_plan()
            else:
                self._apply_decision_wake_plan(decision_json)
        except ActivityError as error:
            self._turn_records.append(
                TurnRecord(
                    turn_id=turn_id,
                    event_ids=event_ids,
                    review_command_ids=review_command_ids,
                    action_attempt_ids=action_attempt_ids,
                    timer_ids=timer_ids,
                    decision_json=None,
                    actions_json=None,
                    execution_results_json=None,
                    error=str(error),
                )
            )
        finally:
            self._turn_in_progress = False
            self._turns_since_continue += 1
            self._completed_turn_count += 1

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

    async def _execute_pending_action(
        self, action_request_id: str, revision: int
    ) -> dict[str, Any] | None:
        self._add_pending_action(action_request_id)
        try:
            activity_result = cast(
                dict[str, Any],
                await workflow.execute_activity(
                    "execute_action",
                    {
                        "tenant_id": self._tenant_id,
                        "process_instance_id": self._process_instance_id,
                        "action_request_id": action_request_id,
                        "revision": revision,
                    },
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                ),
            )
            result_json = str(activity_result["result_json"])
            result = cast(dict[str, Any], json.loads(result_json))
            self._execution_records.append(
                ExecutionRecord(
                    action_request_id=action_request_id,
                    revision=revision,
                    result_json=result_json,
                    error=None,
                )
            )
            if result["status"] == "unknown":
                try:
                    reconciliation_activity = cast(
                        dict[str, Any],
                        await workflow.execute_activity(
                            "reconcile_action",
                            {
                                "tenant_id": self._tenant_id,
                                "process_instance_id": self._process_instance_id,
                                "action_request_id": action_request_id,
                                "revision": revision,
                            },
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        ),
                    )
                    reconciled_json = str(reconciliation_activity["result_json"])
                    result = cast(dict[str, Any], json.loads(reconciled_json))
                    self._execution_records.append(
                        ExecutionRecord(
                            action_request_id=action_request_id,
                            revision=revision,
                            result_json=reconciled_json,
                            error=None,
                        )
                    )
                except ActivityError as reconciliation_error:
                    self._execution_records.append(
                        ExecutionRecord(
                            action_request_id=action_request_id,
                            revision=revision,
                            result_json=None,
                            error=str(reconciliation_error),
                        )
                    )
            if result["status"] in {"succeeded", "failed"}:
                self._remove_pending_action(action_request_id)
            return result
        except ActivityError as error:
            self._execution_records.append(
                ExecutionRecord(
                    action_request_id=action_request_id,
                    revision=revision,
                    result_json=None,
                    error=str(error),
                )
            )
            return None

    def _add_pending_action(self, action_request_id: str) -> None:
        if action_request_id not in self._pending_action_request_ids:
            self._pending_action_request_ids.append(action_request_id)

    def _remove_pending_action(self, action_request_id: str) -> None:
        with suppress(ValueError):
            self._pending_action_request_ids.remove(action_request_id)
