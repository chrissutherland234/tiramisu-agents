"""Tenant-scoped, attributed recovery operations for dead-lettered outbox delivery."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.db.models.events import OutboxMessage, OutboxRecoveryCommand
from tiramisu_agents.db.session import set_tenant_context


class OutboxRecoveryConflict(ValueError):
    """Raised when a delivery cannot be safely requeued."""


@dataclass(frozen=True, slots=True)
class RequeueOutboxInput:
    command_id: UUID
    tenant_id: UUID
    outbox_message_id: UUID
    actor_id: UUID
    reason: str


class OutboxRecoveryService:
    """Reset one exhausted delivery cycle while retaining immutable evidence."""

    async def requeue(
        self, session: AsyncSession, command: RequeueOutboxInput
    ) -> OutboxRecoveryCommand:
        reason = command.reason.strip()
        if not reason or len(reason) > 10_000:
            raise OutboxRecoveryConflict("reason must contain 1 to 10000 characters")
        await set_tenant_context(session, command.tenant_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"outbox-requeue:{command.tenant_id}:{command.command_id}"},
        )
        existing = await session.scalar(
            select(OutboxRecoveryCommand).where(
                OutboxRecoveryCommand.tenant_id == command.tenant_id,
                OutboxRecoveryCommand.id == command.command_id,
            )
        )
        if existing is not None:
            if (
                existing.outbox_message_id != command.outbox_message_id
                or existing.actor_id != command.actor_id
                or existing.reason != reason
            ):
                raise OutboxRecoveryConflict("requeue command ID was reused")
            return existing

        message = await session.scalar(
            select(OutboxMessage)
            .where(
                OutboxMessage.tenant_id == command.tenant_id,
                OutboxMessage.id == command.outbox_message_id,
            )
            .with_for_update()
        )
        if message is None:
            raise OutboxRecoveryConflict("dead-lettered outbox message not found")
        if message.status != "dead_letter" or message.dead_lettered_at is None:
            raise OutboxRecoveryConflict("outbox message is not dead-lettered")

        stored = OutboxRecoveryCommand(
            id=command.command_id,
            tenant_id=command.tenant_id,
            outbox_message_id=command.outbox_message_id,
            actor_id=command.actor_id,
            command_type="requeue",
            reason=reason,
            previous_attempt_count=message.attempt_count,
            previous_error=message.last_error,
            previous_dead_lettered_at=message.dead_lettered_at,
        )
        session.add(stored)
        message.status = "pending"
        message.available_at = datetime.now(UTC)
        message.attempt_count = 0
        message.claimed_at = None
        message.claim_token = None
        message.last_error = None
        message.published_at = None
        message.dead_lettered_at = None
        await session.flush()
        return stored
