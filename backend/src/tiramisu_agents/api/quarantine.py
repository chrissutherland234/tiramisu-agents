"""Tenant-scoped quarantine inspection and audited resolution/replay."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.operator_auth import OperatorIdentity, require_operator_identity
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.events import EventInbox, EventResolutionCommand, ExternalCorrelation
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.events.ingestion import TenantNotFound
from tiramisu_agents.events.quarantine import (
    QuarantineConflict,
    QuarantineNotFound,
    QuarantineResolutionService,
    ResolveQuarantineInput,
    reference_key,
)
from tiramisu_agents.security.credentials import CredentialScope
from tiramisu_agents.security.tenancy import TenantNotAuthorized, TenantSuspended

router = APIRouter(prefix="/v1/quarantine", tags=["quarantine"])


class ResolutionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    event_id: UUID
    process_instance_id: UUID
    actor_id: UUID
    reason: str
    previous_status: str
    previous_reason: str | None
    bound_references: tuple[ExternalReference, ...]
    delivery_scheduled: bool
    created_at: datetime


class QuarantineSummary(BaseModel):
    id: UUID
    event_type: str
    source: str
    source_event_id: str
    correlation_status: str
    correlation_reason: str | None
    process_instance_id: UUID | None
    received_at: datetime
    resolution: ResolutionSummary | None


class QuarantinePage(BaseModel):
    items: list[QuarantineSummary]
    total: int
    limit: int
    offset: int
    can_resolve: bool


class ReferenceSummary(BaseModel):
    reference: ExternalReference
    process_instance_id: UUID | None


class CandidateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    process_type: str
    status: str
    deployment_id: str


class QuarantineDetail(QuarantineSummary):
    event: CanonicalEvent
    references: list[ReferenceSummary]
    candidates: list[CandidateSummary]
    can_resolve: bool


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    process_instance_id: UUID
    reason: str = Field(min_length=1, max_length=10_000)
    bind_references: tuple[ExternalReference, ...] = Field(default=(), max_length=100)


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def _can_resolve(identity: OperatorIdentity) -> bool:
    return "*" in identity.scopes or CredentialScope.QUARANTINE_RESOLVE in identity.scopes


def _summary(event: EventInbox, resolution: EventResolutionCommand | None) -> QuarantineSummary:
    return QuarantineSummary(
        id=event.id,
        event_type=event.event_type,
        source=event.source,
        source_event_id=event.source_event_id,
        correlation_status=event.correlation_status,
        correlation_reason=event.correlation_reason,
        process_instance_id=event.process_instance_id,
        received_at=event.received_at,
        resolution=ResolutionSummary.model_validate(resolution) if resolution else None,
    )


@router.get("", response_model=QuarantinePage)
async def list_quarantine(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
    state: Literal["unresolved", "resolved"] = "unresolved",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QuarantinePage:
    identity.require_scope(CredentialScope.QUARANTINE_READ)
    query = (
        select(EventInbox, EventResolutionCommand)
        .outerjoin(
            EventResolutionCommand,
            and_(
                EventResolutionCommand.tenant_id == EventInbox.tenant_id,
                EventResolutionCommand.event_id == EventInbox.id,
            ),
        )
        .where(EventInbox.tenant_id == identity.tenant_id)
    )
    if state == "unresolved":
        query = query.where(EventInbox.correlation_status.in_(("pending", "rejected")))
        ordering = EventInbox.received_at.desc()
    else:
        query = query.where(EventResolutionCommand.id.is_not(None))
        ordering = EventResolutionCommand.created_at.desc()
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await session.execute(
                query.order_by(ordering, EventInbox.id).limit(limit).offset(offset)
            )
        ).all()
        return QuarantinePage(
            items=[_summary(event, resolution) for event, resolution in rows],
            total=total or 0,
            limit=limit,
            offset=offset,
            can_resolve=_can_resolve(identity),
        )


@router.get("/{event_id}", response_model=QuarantineDetail)
async def get_quarantine(
    event_id: UUID,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> QuarantineDetail:
    identity.require_scope(CredentialScope.QUARANTINE_READ)
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        row = (
            await session.execute(
                select(EventInbox, EventResolutionCommand)
                .outerjoin(
                    EventResolutionCommand,
                    and_(
                        EventResolutionCommand.tenant_id == EventInbox.tenant_id,
                        EventResolutionCommand.event_id == EventInbox.id,
                    ),
                )
                .where(EventInbox.tenant_id == identity.tenant_id, EventInbox.id == event_id)
            )
        ).one_or_none()
        if row is None or (row[0].correlation_status == "matched" and row[1] is None):
            raise HTTPException(status_code=404, detail="quarantined event not found")
        inbox, resolution = row
        event = CanonicalEvent.model_validate(inbox.event_data)
        predicates = [
            and_(
                ExternalCorrelation.provider == ref.provider,
                ExternalCorrelation.resource_type == ref.resource_type,
                ExternalCorrelation.external_id == ref.external_id,
            )
            for ref in event.external_references
        ]
        owners = {}
        if predicates:
            correlations = (
                await session.scalars(
                    select(ExternalCorrelation).where(
                        ExternalCorrelation.tenant_id == identity.tenant_id, or_(*predicates)
                    )
                )
            ).all()
            owners = {
                (ref.provider, ref.resource_type, ref.external_id): ref.process_instance_id
                for ref in correlations
            }
        candidate_ids = set(owners.values())
        if event.process_instance_id:
            candidate_ids.add(event.process_instance_id)
        if inbox.process_instance_id:
            candidate_ids.add(inbox.process_instance_id)
        candidates = (
            await session.scalars(
                select(ProcessInstance)
                .where(
                    ProcessInstance.tenant_id == identity.tenant_id,
                    ProcessInstance.id.in_(candidate_ids),
                )
                .order_by(ProcessInstance.id)
            )
        ).all()
        return QuarantineDetail(
            **_summary(inbox, resolution).model_dump(),
            event=event,
            references=[
                ReferenceSummary(reference=ref, process_instance_id=owners.get(reference_key(ref)))
                for ref in dict.fromkeys(event.external_references)
            ],
            candidates=[CandidateSummary.model_validate(candidate) for candidate in candidates],
            can_resolve=_can_resolve(identity),
        )


@router.post(
    "/{event_id}/resolve", response_model=ResolutionSummary, status_code=status.HTTP_202_ACCEPTED
)
async def resolve_quarantine(
    event_id: UUID,
    body: ResolveRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> ResolutionSummary:
    identity.require_scope(CredentialScope.QUARANTINE_RESOLVE)
    try:
        async with _session_factory(request).begin() as session:
            release = request.app.state.deployment_release
            stored = await QuarantineResolutionService().resolve(
                session,
                ResolveQuarantineInput(
                    command_id=body.command_id,
                    tenant_id=identity.tenant_id,
                    event_id=event_id,
                    process_instance_id=body.process_instance_id,
                    actor_id=identity.actor_id,
                    reason=body.reason,
                    bind_references=body.bind_references,
                ),
                deployment_id=release.deployment_id if release else None,
            )
            return ResolutionSummary.model_validate(stored)
    except (QuarantineNotFound, TenantNotFound) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TenantSuspended, TenantNotAuthorized) as error:
        raise HTTPException(status_code=403, detail="tenant cannot resolve events here") from error
    except QuarantineConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="resolution identity conflicts") from error
