"""Development event-ingestion API backed by the durable inbox."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.operator_auth import (
    OperatorIdentity,
    require_event_ingress_identity,
)
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference, Sensitivity
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.core.limits import (
    DEFAULT_PLATFORM_SAFETY_LIMITS,
    require_event_content,
    require_json_bytes,
)
from tiramisu_agents.events.ingestion import (
    EventIngestionService,
    ProcessBootstrap,
    ReservedKernelEventType,
    TenantNotFound,
    TriggerReferenceRequired,
)
from tiramisu_agents.extensions import DeploymentRelease
from tiramisu_agents.security.tenancy import TenantNotAuthorized, TenantSuspended

router = APIRouter(prefix="/v1/events", tags=["events"])


class IngestEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    process_instance_id: UUID | None = None
    event_type: str = Field(
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
        max_length=150,
    )
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

    @model_validator(mode="after")
    def require_bounded_content(self) -> "IngestEventRequest":
        limits = DEFAULT_PLATFORM_SAFETY_LIMITS
        require_event_content(
            payload=self.payload,
            external_references=self.external_references,
            facts=self.facts,
            limits=limits,
        )
        require_json_bytes(
            self.model_dump(mode="json"),
            label="event input",
            max_bytes=limits.max_event_input_bytes,
        )
        return self


class IngestEventResponse(BaseModel):
    event_id: UUID
    created: bool
    correlation_status: str
    correlation_reason: str | None
    process_instance_id: UUID | None
    delivery_scheduled: bool


def fictional_trigger_rules() -> dict[str, ProcessBootstrap]:
    client_pack = load_fictional_deployment()
    release = DeploymentRelease(
        deployment_id="fictional-local",
        build_id="test-helper",
        client_pack_fingerprint=client_pack.fingerprint(),
        model_id="test-model",
    )
    return client_pack.trigger_rules(release)


@router.post("", response_model=IngestEventResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(
    body: IngestEventRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_event_ingress_identity)],
) -> IngestEventResponse:
    event = CanonicalEvent(
        event_id=body.event_id,
        tenant_id=identity.tenant_id,
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
                deployment_id=(
                    request.app.state.deployment_release.deployment_id
                    if request.app.state.deployment_release is not None
                    else None
                ),
            )
    except TenantNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found"
        ) from error
    except TenantSuspended as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant is suspended",
        ) from error
    except TenantNotAuthorized as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant is not assigned to this deployment",
        ) from error
    except (ReservedKernelEventType, TriggerReferenceRequired) as error:
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
