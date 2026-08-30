"""Transactional canonical event ingestion, correlation, and outbox creation."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import set_tenant_context


@dataclass(frozen=True, slots=True)
class ProcessBootstrap:
    """Trusted process configuration selected outside the incoming event payload."""

    process_type: str
    definition_version: str
    extension_manifest_hash: str


@dataclass(frozen=True, slots=True)
class IngestionResult:
    event_id: UUID
    created: bool
    correlation_status: str
    correlation_reason: str | None
    process_instance_id: UUID | None
    outbox_message_id: UUID | None


class TenantNotFound(LookupError):
    """Raised before ingestion when the selected tenant does not exist or is invisible."""


class TriggerReferenceRequired(ValueError):
    """Raised when a configured trigger cannot establish a durable correlation."""


class EventIngestionService:
    """Persist one canonical event and atomically schedule matched delivery."""

    async def ingest(
        self,
        session: AsyncSession,
        event: CanonicalEvent,
        *,
        bootstrap: ProcessBootstrap | None = None,
    ) -> IngestionResult:
        await set_tenant_context(session, event.tenant_id)
        if await session.scalar(select(Tenant.id).where(Tenant.id == event.tenant_id)) is None:
            raise TenantNotFound(str(event.tenant_id))

        await self._lock_source_event_key(session, event)
        existing = await self._existing_result(session, event)
        if existing is not None:
            return existing

        if bootstrap is not None and not event.external_references:
            raise TriggerReferenceRequired(
                "a process-triggering event requires an external reference"
            )
        if event.external_references:
            await self._lock_correlation_keys(session, event.tenant_id, event.external_references)

        process_id, status, reason = await self._resolve_process(session, event)
        if process_id is None and status == "pending" and bootstrap is not None:
            process_id = await self._create_process(session, event.tenant_id, bootstrap)
            status = "matched"
            reason = "process_created_from_trigger"
        if process_id is not None and status == "matched":
            await self._persist_references(
                session,
                tenant_id=event.tenant_id,
                process_id=process_id,
                references=event.external_references,
            )

        inserted_id = await session.scalar(
            insert(EventInbox)
            .values(
                id=event.event_id,
                tenant_id=event.tenant_id,
                process_instance_id=process_id,
                source=event.source,
                source_event_id=event.source_event_id,
                event_type=event.event_type,
                event_data=event.model_dump(mode="json"),
                correlation_status=status,
                correlation_reason=reason,
                received_at=event.received_at,
            )
            .on_conflict_do_nothing(constraint="uq_event_inbox_source_event")
            .returning(EventInbox.id)
        )
        if inserted_id is None:
            existing = await self._existing_result(session, event)
            if existing is None:
                raise RuntimeError("event ID conflicts with a different source event")
            return existing

        outbox_id: UUID | None = None
        if process_id is not None and status == "matched":
            workflow_id = await session.scalar(
                select(ProcessInstance.workflow_id).where(ProcessInstance.id == process_id)
            )
            if workflow_id is None:
                raise RuntimeError("matched process has no workflow identity")
            outbox_id = uuid4()
            await session.execute(
                insert(OutboxMessage)
                .values(
                    id=outbox_id,
                    tenant_id=event.tenant_id,
                    process_instance_id=process_id,
                    causation_event_id=event.event_id,
                    message_type="temporal.process_event",
                    destination=workflow_id,
                    deduplication_key=f"process-event:{event.event_id}",
                    payload={"event_id": str(event.event_id), "event_type": event.event_type},
                )
                .on_conflict_do_nothing(constraint="uq_outbox_messages_dedup")
            )

        return IngestionResult(
            event_id=inserted_id,
            created=True,
            correlation_status=status,
            correlation_reason=reason,
            process_instance_id=process_id,
            outbox_message_id=outbox_id,
        )

    async def _existing_result(
        self, session: AsyncSession, event: CanonicalEvent
    ) -> IngestionResult | None:
        row = (
            await session.execute(
                select(
                    EventInbox.id,
                    EventInbox.correlation_status,
                    EventInbox.correlation_reason,
                    EventInbox.process_instance_id,
                    OutboxMessage.id.label("outbox_id"),
                )
                .outerjoin(
                    OutboxMessage,
                    and_(
                        OutboxMessage.tenant_id == EventInbox.tenant_id,
                        OutboxMessage.causation_event_id == EventInbox.id,
                    ),
                )
                .where(
                    EventInbox.source == event.source,
                    EventInbox.source_event_id == event.source_event_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return IngestionResult(
            event_id=row.id,
            created=False,
            correlation_status=row.correlation_status,
            correlation_reason=row.correlation_reason,
            process_instance_id=row.process_instance_id,
            outbox_message_id=row.outbox_id,
        )

    async def _resolve_process(
        self, session: AsyncSession, event: CanonicalEvent
    ) -> tuple[UUID | None, str, str]:
        candidates: set[UUID] = set()
        if event.process_instance_id is not None:
            explicit = await session.scalar(
                select(ProcessInstance.id).where(ProcessInstance.id == event.process_instance_id)
            )
            if explicit is None:
                return None, "rejected", "explicit_process_not_found"
            candidates.add(explicit)

        if event.external_references:
            predicates = [
                and_(
                    ExternalCorrelation.provider == reference.provider,
                    ExternalCorrelation.resource_type == reference.resource_type,
                    ExternalCorrelation.external_id == reference.external_id,
                )
                for reference in self._unique_references(event.external_references)
            ]
            candidates.update(
                (
                    await session.scalars(
                        select(ExternalCorrelation.process_instance_id).where(or_(*predicates))
                    )
                ).all()
            )

        if len(candidates) == 1:
            return candidates.pop(), "matched", "single_process_match"
        if len(candidates) > 1:
            return None, "pending", "ambiguous_external_references"
        return None, "pending", "no_process_match"

    async def _create_process(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        bootstrap: ProcessBootstrap,
    ) -> UUID:
        process_id = uuid4()
        session.add(
            ProcessInstance(
                id=process_id,
                tenant_id=tenant_id,
                process_type=bootstrap.process_type,
                definition_version=bootstrap.definition_version,
                extension_manifest_hash=bootstrap.extension_manifest_hash,
                status="active",
                workflow_id=f"tenant/{tenant_id}/process/{process_id}",
            )
        )
        await session.flush()
        return process_id

    async def _persist_references(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_id: UUID,
        references: tuple[ExternalReference, ...],
    ) -> None:
        unique_references = self._unique_references(references)
        if not unique_references:
            return
        predicates = [
            and_(
                ExternalCorrelation.provider == reference.provider,
                ExternalCorrelation.resource_type == reference.resource_type,
                ExternalCorrelation.external_id == reference.external_id,
            )
            for reference in unique_references
        ]
        existing = {
            (row.provider, row.resource_type, row.external_id): row.process_instance_id
            for row in (
                await session.execute(
                    select(
                        ExternalCorrelation.provider,
                        ExternalCorrelation.resource_type,
                        ExternalCorrelation.external_id,
                        ExternalCorrelation.process_instance_id,
                    ).where(or_(*predicates))
                )
            ).all()
        }
        additions: list[ExternalCorrelation] = []
        for reference in unique_references:
            key = (reference.provider, reference.resource_type, reference.external_id)
            owner = existing.get(key)
            if owner is not None and owner != process_id:
                raise RuntimeError("external reference changed ownership during correlation")
            if owner is None:
                additions.append(
                    ExternalCorrelation(
                        tenant_id=tenant_id,
                        process_instance_id=process_id,
                        provider=reference.provider,
                        resource_type=reference.resource_type,
                        external_id=reference.external_id,
                    )
                )
        session.add_all(additions)
        if additions:
            await session.flush()

    async def _lock_source_event_key(self, session: AsyncSession, event: CanonicalEvent) -> None:
        key = f"event:{event.tenant_id}:{event.source}:{event.source_event_id}"
        await self._advisory_lock(session, key)

    async def _lock_correlation_keys(
        self,
        session: AsyncSession,
        tenant_id: UUID,
        references: tuple[ExternalReference, ...],
    ) -> None:
        keys = sorted(
            f"{tenant_id}:{reference.provider}:{reference.resource_type}:{reference.external_id}"
            for reference in self._unique_references(references)
        )
        for key in keys:
            await self._advisory_lock(session, key)

    async def _advisory_lock(self, session: AsyncSession, key: str) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )

    @staticmethod
    def _unique_references(
        references: tuple[ExternalReference, ...],
    ) -> tuple[ExternalReference, ...]:
        unique: dict[tuple[str, str, str], ExternalReference] = {}
        for reference in references:
            key = (reference.provider, reference.resource_type, reference.external_id)
            unique[key] = reference
        return tuple(unique.values())
