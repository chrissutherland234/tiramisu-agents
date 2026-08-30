"""Development event-ingestion API backed by the durable inbox."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.settings import Settings
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference, Sensitivity
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.events.ingestion import (
    EventIngestionService,
    ProcessBootstrap,
    TenantNotFound,
    TriggerReferenceRequired,
)

router = APIRouter(prefix="/v1/events", tags=["events"])


class IngestEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    process_instance_id: UUID | None = None
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    source: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(min_length=1, max_length=500)
    occurred_at: datetime
    schema_version: int = Field(default=1, ge=1)
    sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL
    external_references: tuple[ExternalReference, ...] = ()
    facts: tuple[FactObservation, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        return value


class IngestEventResponse(BaseModel):
    event_id: UUID
    created: bool
    correlation_status: str
    correlation_reason: str | None
    process_instance_id: UUID | None
    delivery_scheduled: bool


def fictional_trigger_rules() -> dict[str, ProcessBootstrap]:
    return load_fictional_deployment().trigger_rules()


@router.post("", response_model=IngestEventResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(
    body: IngestEventRequest,
    request: Request,
    tenant_header: Annotated[str | None, Header(alias="X-Tiramisu-Tenant-ID")] = None,
) -> IngestEventResponse:
    settings: Settings = request.app.state.settings
    if settings.environment != "development" or not settings.allow_unsafe_development_tenant_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="development tenant-header ingestion is disabled",
        )
    if tenant_header is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tiramisu-Tenant-ID is required",
        )
    try:
        tenant_id = UUID(tenant_header)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tiramisu-Tenant-ID must be a UUID",
        ) from error

    event = CanonicalEvent(
        event_id=body.event_id,
        tenant_id=tenant_id,
        process_instance_id=body.process_instance_id,
        event_type=body.event_type,
        source=body.source,
        source_event_id=body.source_event_id,
        occurred_at=body.occurred_at,
        schema_version=body.schema_version,
        sensitivity=body.sensitivity,
        external_references=body.external_references,
        facts=body.facts,
        payload=body.payload,
    )
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    trigger_rules: dict[str, ProcessBootstrap] = request.app.state.trigger_rules
    try:
        async with session_factory.begin() as session:
            result = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=trigger_rules.get(event.event_type),
            )
    except TenantNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found"
        ) from error
    except TriggerReferenceRequired as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return IngestEventResponse(
        event_id=result.event_id,
        created=result.created,
        correlation_status=result.correlation_status,
        correlation_reason=result.correlation_reason,
        process_instance_id=result.process_instance_id,
        delivery_scheduled=result.outbox_message_id is not None,
    )
