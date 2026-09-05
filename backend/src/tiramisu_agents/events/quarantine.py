"""Audited resolution of quarantined events through the normal delivery outbox."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.core.reserved_events import RESERVED_KERNEL_EVENT_TYPES
from tiramisu_agents.db.models.events import EventInbox, EventResolutionCommand
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.events.ingestion import EventIngestionService


class QuarantineNotFound(LookupError):
    """The requested event or destination is not visible to this tenant."""


class QuarantineConflict(ValueError):
    """Resolution would change an existing decision or reference ownership."""


@dataclass(frozen=True, slots=True)
class ResolveQuarantineInput:
    command_id: UUID
    tenant_id: UUID
    event_id: UUID
    process_instance_id: UUID
    actor_id: UUID
    reason: str
    bind_references: tuple[ExternalReference, ...] = ()


def reference_key(reference: ExternalReference) -> tuple[str, str, str]:
    return reference.provider, reference.resource_type, reference.external_id


class QuarantineResolutionService:
    async def resolve(
        self,
        session: AsyncSession,
        command: ResolveQuarantineInput,
        *,
        deployment_id: str | None = None,
    ) -> EventResolutionCommand:
        reason = command.reason.strip()
        if not reason or len(reason) > 10_000:
            raise QuarantineConflict("reason must contain 1 to 10000 characters")
        references = tuple(sorted(set(command.bind_references), key=reference_key))
        serialized_references = [reference.model_dump(mode="json") for reference in references]
        ingestion = EventIngestionService()
        assigned_deployment = await ingestion.require_ingress_tenant(
            session, command.tenant_id, deployment_id=deployment_id
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"quarantine-command:{command.tenant_id}:{command.command_id}"},
        )
        existing = await session.scalar(
            select(EventResolutionCommand).where(
                EventResolutionCommand.tenant_id == command.tenant_id,
                EventResolutionCommand.id == command.command_id,
            )
        )
        if existing is not None:
            if (
                existing.event_id != command.event_id
                or existing.process_instance_id != command.process_instance_id
                or existing.actor_id != command.actor_id
                or existing.reason != reason
                or existing.bound_references != serialized_references
            ):
                raise QuarantineConflict("resolution command ID was reused with different content")
            return existing

        row = await session.scalar(
            select(EventInbox).where(
                EventInbox.tenant_id == command.tenant_id, EventInbox.id == command.event_id
            )
        )
        if row is None:
            raise QuarantineNotFound("quarantined event not found")
        event = CanonicalEvent.model_validate(row.event_data)
        if event.event_type in RESERVED_KERNEL_EVENT_TYPES:
            raise QuarantineConflict("kernel events cannot be replayed through quarantine")
        if not {reference_key(ref) for ref in references}.issubset(
            reference_key(ref) for ref in event.external_references
        ):
            raise QuarantineConflict("only references from the original event can be bound")

        # Match ingress lock order: source identity, reference identities, process.
        # Lock the entire event reference set, including ones deliberately left unbound.
        await ingestion.lock_source_event_key(session, event)
        await ingestion.lock_correlation_keys(session, command.tenant_id, event.external_references)
        row = await session.scalar(
            select(EventInbox)
            .where(EventInbox.tenant_id == command.tenant_id, EventInbox.id == command.event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise QuarantineNotFound("quarantined event not found")
        if row.correlation_status not in {"pending", "rejected"} or row.process_instance_id:
            raise QuarantineConflict("event is already correlated; use delivery recovery if needed")
        process = await session.scalar(
            select(ProcessInstance)
            .where(
                ProcessInstance.tenant_id == command.tenant_id,
                ProcessInstance.id == command.process_instance_id,
            )
            .with_for_update()
        )
        if process is None:
            raise QuarantineNotFound("destination process not found")
        if process.deployment_id != assigned_deployment:
            raise QuarantineConflict("destination process belongs to another deployment")
        terminal = process.status in {"completed", "cancelled", "failed"}
        if terminal and process.late_event_policy != "record_only":
            raise QuarantineConflict("unsupported terminal late-event policy")
        try:
            await ingestion.persist_references(
                session,
                tenant_id=command.tenant_id,
                process_id=process.id,
                references=references,
            )
        except RuntimeError as error:
            raise QuarantineConflict(
                "a selected reference belongs to another process; its ownership cannot change"
            ) from error

        stored = EventResolutionCommand(
            id=command.command_id,
            tenant_id=command.tenant_id,
            event_id=command.event_id,
            process_instance_id=process.id,
            actor_id=command.actor_id,
            reason=reason,
            previous_status=row.correlation_status,
            previous_reason=row.correlation_reason,
            bound_references=serialized_references,
            delivery_scheduled=not terminal,
        )
        session.add(stored)
        row.process_instance_id = process.id
        row.correlation_status = "matched"
        row.correlation_reason = (
            "terminal_process_record_only" if terminal else "operator_quarantine_resolution"
        )
        # event_data is deliberately immutable; trusted context loading overlays
        # the resolved process ID from the inbox column, as for ordinary ingress.
        await session.flush()
        if not terminal:
            await ingestion.schedule_delivery(session, event, process.id)
        return stored
