"""Recoverable PostgreSQL outbox delivery to Temporal."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from tiramisu_agents.db.models.events import OutboxMessage
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.temporal.workflows.mailbox import (
    MailboxEvent,
    MailboxInput,
    ProcessMailboxWorkflow,
)


class DispatchStatus(StrEnum):
    EMPTY = "empty"
    PUBLISHED = "published"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ClaimedMessage:
    id: UUID
    tenant_id: UUID
    process_instance_id: UUID
    destination: str
    payload: dict[str, Any]
    attempt_count: int


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
    ) -> None:
        self._session_factory = session_factory
        self._temporal_client = temporal_client
        self._task_queue = task_queue
        self._max_attempts = max_attempts
        self._stale_claim_after = stale_claim_after
        self._retry_base_delay = retry_base_delay

    async def dispatch_one(self, tenant_id: UUID) -> DispatchResult:
        now = datetime.now(UTC)
        async with self._session_factory.begin() as session:
            message = await self._claim(session, tenant_id, now=now)
        if message is None:
            return DispatchResult(status=DispatchStatus.EMPTY)

        try:
            await self._temporal_client.start_workflow(
                ProcessMailboxWorkflow.run,
                MailboxInput(
                    tenant_id=str(message.tenant_id),
                    process_instance_id=str(message.process_instance_id),
                ),
                id=message.destination,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                start_signal="receive_event",
                start_signal_args=[
                    MailboxEvent(
                        event_id=str(message.payload["event_id"]),
                        event_type=str(message.payload["event_type"]),
                    )
                ],
            )
        except Exception as error:
            return await self._record_failure(message, error)

        async with self._session_factory.begin() as session:
            await set_tenant_context(session, message.tenant_id)
            stored = await session.get(OutboxMessage, message.id, with_for_update=True)
            if stored is None:
                raise RuntimeError("claimed outbox message disappeared")
            stored.status = "published"
            stored.published_at = datetime.now(UTC)
            stored.claimed_at = None
            stored.last_error = None
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
        stale_before = now - self._stale_claim_after
        stored = await session.scalar(
            select(OutboxMessage)
            .where(
                OutboxMessage.message_type == "temporal.process_event",
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
        stored.status = "publishing"
        stored.claimed_at = now
        stored.attempt_count += 1
        return ClaimedMessage(
            id=stored.id,
            tenant_id=stored.tenant_id,
            process_instance_id=stored.process_instance_id,
            destination=stored.destination,
            payload=stored.payload,
            attempt_count=stored.attempt_count,
        )

    async def _record_failure(self, message: ClaimedMessage, error: Exception) -> DispatchResult:
        error_text = f"{type(error).__name__}: {error}"[:2000]
        terminal = message.attempt_count >= self._max_attempts
        async with self._session_factory.begin() as session:
            await set_tenant_context(session, message.tenant_id)
            stored = await session.get(OutboxMessage, message.id, with_for_update=True)
            if stored is None:
                raise RuntimeError("claimed outbox message disappeared")
            stored.status = "failed" if terminal else "pending"
            stored.claimed_at = None
            stored.last_error = error_text
            if not terminal:
                multiplier = 2 ** (message.attempt_count - 1)
                stored.available_at = datetime.now(UTC) + self._retry_base_delay * multiplier
        return DispatchResult(
            status=DispatchStatus.FAILED if terminal else DispatchStatus.RETRY_SCHEDULED,
            message_id=message.id,
            error=error_text,
        )
