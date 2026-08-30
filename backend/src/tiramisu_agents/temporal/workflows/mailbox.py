"""A deterministic mailbox that owns one durable business process identity."""

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow


@dataclass(frozen=True)
class MailboxInput:
    tenant_id: str
    process_instance_id: str


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
class MailboxState:
    tenant_id: str
    process_instance_id: str
    buffered_events: tuple[MailboxEvent, ...]
    buffered_reviews: tuple[MailboxReview, ...]
    wake_records: tuple[WakeRecord, ...]
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
        self._wake_plan: WakePlan | None = None
        self._timer_due_at: datetime | None = None
        self._plan_revision = 0
        self._closed = False

    @workflow.run
    async def run(self, workflow_input: MailboxInput) -> MailboxState:
        self._tenant_id = workflow_input.tenant_id
        self._process_instance_id = workflow_input.process_instance_id

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
                continue

            revision = self._plan_revision
            timeout = self._timer_timeout()
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda awaited_revision=revision: (
                        self._closed
                        or self._plan_revision != awaited_revision
                        or bool(self._buffered_reviews)
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
