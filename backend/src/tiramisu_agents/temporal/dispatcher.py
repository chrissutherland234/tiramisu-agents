"""Recoverable PostgreSQL outbox delivery to Temporal."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from tiramisu_agents.db.models.events import OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxActionResolution,
    MailboxControl,
    MailboxEvent,
    MailboxInput,
    MailboxReview,
    ProcessMailboxWorkflow,
)


class DispatchStatus(StrEnum):
    EMPTY = "empty"
    PUBLISHED = "published"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    CLAIM_LOST = "claim_lost"


@dataclass(frozen=True, slots=True)
class ClaimedMessage:
    id: UUID
    tenant_id: UUID
    process_instance_id: UUID
    message_type: str
    destination: str
    payload: dict[str, Any]
    attempt_count: int
    claim_token: UUID
    process_definition_id: str | None
    process_definition_version: str | None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    status: DispatchStatus
    message_id: UUID | None = None
    error: str | None = None


class TemporalOutboxDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        temporal_client: Client,
        *,
        task_queue: str,
        max_attempts: int = 5,
        stale_claim_after: timedelta = timedelta(minutes=5),
        retry_base_delay: timedelta = timedelta(seconds=5),
        orchestrate_agent_turns: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._temporal_client = temporal_client
        self._task_queue = task_queue
        self._max_attempts = max_attempts
        self._stale_claim_after = stale_claim_after
        self._retry_base_delay = retry_base_delay
        self._orchestrate_agent_turns = orchestrate_agent_turns

    async def dispatch_one(self, tenant_id: UUID) -> DispatchResult:
        now = datetime.now(UTC)
        async with self._session_factory.begin() as session:
            message = await self._claim(session, tenant_id, now=now)
        if message is None:
            return DispatchResult(status=DispatchStatus.EMPTY)

        try:
            if message.message_type == "temporal.process_event":
                signal_name = "receive_event"
                signal_argument: (
                    MailboxEvent | MailboxReview | MailboxActionResolution | MailboxControl
                ) = MailboxEvent(
                    event_id=str(message.payload["event_id"]),
                    event_type=str(message.payload["event_type"]),
                )
            elif message.message_type == "temporal.process_review":
                signal_name = "receive_review"
                signal_argument = MailboxReview(
                    command_id=str(message.payload["command_id"]),
                    command_type=str(message.payload["command_type"]),
                    review_thread_id=str(message.payload["review_thread_id"]),
                    action_request_id=str(message.payload["action_request_id"]),
                    proposal_revision=int(message.payload["proposal_revision"]),
                )
            elif message.message_type == "temporal.action_resolution":
                signal_name = "receive_action_resolution"
                signal_argument = MailboxActionResolution(
                    command_id=str(message.payload["command_id"]),
                    action_request_id=str(message.payload["action_request_id"]),
                    action_attempt_id=str(message.payload["action_attempt_id"]),
                    status=str(message.payload["status"]),
                )
            elif message.message_type == "temporal.process_control":
                signal_name = "receive_control"
                signal_argument = MailboxControl(
                    command_id=str(message.payload["command_id"]),
                    command_type=str(message.payload["command_type"]),
                    event_ids=tuple(str(value) for value in message.payload.get("event_ids", ())),
                    review_command_ids=tuple(
                        str(value) for value in message.payload.get("review_command_ids", ())
                    ),
                    action_attempt_ids=tuple(
                        str(value) for value in message.payload.get("action_attempt_ids", ())
                    ),
                    timer_ids=tuple(str(value) for value in message.payload.get("timer_ids", ())),
                )
            else:
                raise RuntimeError(f"unsupported Temporal message type: {message.message_type}")
            await self._temporal_client.start_workflow(
                ProcessMailboxWorkflow.run,
                MailboxInput(
                    tenant_id=str(message.tenant_id),
                    process_instance_id=str(message.process_instance_id),
                    process_definition_id=message.process_definition_id,
                    process_definition_version=message.process_definition_version,
                ),
                id=message.destination,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                start_signal=signal_name,
                start_signal_args=[signal_argument],
            )
        except Exception as error:
            return await self._record_failure(message, error)

        async with self._session_factory.begin() as session:
            await set_tenant_context(session, message.tenant_id)
            completed_id = await session.scalar(
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message.id,
                    OutboxMessage.status == "publishing",
                    OutboxMessage.claim_token == message.claim_token,
                )
                .values(
                    status="published",
                    published_at=datetime.now(UTC),
                    dead_lettered_at=None,
                    claimed_at=None,
                    claim_token=None,
                    last_error=None,
                )
                .returning(OutboxMessage.id)
            )
        if completed_id is None:
            return DispatchResult(status=DispatchStatus.CLAIM_LOST, message_id=message.id)
        return DispatchResult(status=DispatchStatus.PUBLISHED, message_id=message.id)

    async def run_tenant(
        self, tenant_id: UUID, *, poll_interval: timedelta = timedelta(seconds=1)
    ) -> None:
        """Continuously dispatch for one deployment-authorized tenant."""
        while True:
            result = await self.dispatch_one(tenant_id)
            if result.status is not DispatchStatus.PUBLISHED:
                await asyncio.sleep(poll_interval.total_seconds())

    async def _claim(
        self, session: AsyncSession, tenant_id: UUID, *, now: datetime
    ) -> ClaimedMessage | None:
        await set_tenant_context(session, tenant_id)
        tenant_status = await session.scalar(select(Tenant.status).where(Tenant.id == tenant_id))
        if tenant_status != "active":
            return None
        stale_before = now - self._stale_claim_after
        stored = await session.scalar(
            select(OutboxMessage)
            .where(
                OutboxMessage.message_type.in_(
                    (
                        "temporal.process_event",
                        "temporal.process_review",
                        "temporal.action_resolution",
                        "temporal.process_control",
                    )
                ),
                OutboxMessage.available_at <= now,
                or_(
                    OutboxMessage.status == "pending",
                    and_(
                        OutboxMessage.status == "publishing",
                        OutboxMessage.claimed_at < stale_before,
                    ),
                ),
            )
            .order_by(OutboxMessage.available_at, OutboxMessage.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if stored is None:
            return None
        if stored.process_instance_id is None:
            raise RuntimeError("Temporal outbox message has no process instance")
        definition_id: str | None = None
        definition_version: str | None = None
        if self._orchestrate_agent_turns:
            process = await session.scalar(
                select(ProcessInstance).where(ProcessInstance.id == stored.process_instance_id)
            )
            if process is None:
                raise RuntimeError("Temporal outbox process instance is unavailable")
            definition_id = process.process_type
            definition_version = process.definition_version
        stored.status = "publishing"
        stored.claimed_at = now
        stored.claim_token = uuid4()
        stored.attempt_count += 1
        claim_token = stored.claim_token
        if claim_token is None:
            raise RuntimeError("outbox claim token was not assigned")
        return ClaimedMessage(
            id=stored.id,
            tenant_id=stored.tenant_id,
            process_instance_id=stored.process_instance_id,
            message_type=stored.message_type,
            destination=stored.destination,
            payload=stored.payload,
            attempt_count=stored.attempt_count,
            claim_token=claim_token,
            process_definition_id=definition_id,
            process_definition_version=definition_version,
        )

    async def _record_failure(self, message: ClaimedMessage, error: Exception) -> DispatchResult:
        error_text = f"{type(error).__name__}: {error}"[:2000]
        terminal = message.attempt_count >= self._max_attempts
        next_available_at = datetime.now(UTC)
        if not terminal:
            multiplier = 2 ** (message.attempt_count - 1)
            next_available_at += self._retry_base_delay * multiplier
        async with self._session_factory.begin() as session:
            await set_tenant_context(session, message.tenant_id)
            updated_id = await session.scalar(
                update(OutboxMessage)
                .where(
                    OutboxMessage.id == message.id,
                    OutboxMessage.status == "publishing",
                    OutboxMessage.claim_token == message.claim_token,
                )
                .values(
                    status="dead_letter" if terminal else "pending",
                    claimed_at=None,
                    claim_token=None,
                    last_error=error_text,
                    available_at=next_available_at,
                    dead_lettered_at=datetime.now(UTC) if terminal else None,
                )
                .returning(OutboxMessage.id)
            )
        if updated_id is None:
            return DispatchResult(
                status=DispatchStatus.CLAIM_LOST,
                message_id=message.id,
                error=error_text,
            )
        return DispatchResult(
            status=(DispatchStatus.DEAD_LETTERED if terminal else DispatchStatus.RETRY_SCHEDULED),
            message_id=message.id,
            error=error_text,
        )
