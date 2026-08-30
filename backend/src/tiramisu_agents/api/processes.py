"""Tenant-scoped development operator views and review commands."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.operator_auth import (
    OperatorIdentity,
    require_operator_identity,
    require_process_reader,
    require_review_reader,
)
from tiramisu_agents.core.contracts.decisions import WakeCondition
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import ProcessInstance, ProcessStateRevision
from tiramisu_agents.db.models.reviews import ReviewMessage, ReviewThread
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.reviews.service import ReviewConflict, ReviewService
from tiramisu_agents.security.credentials import CredentialScope

router = APIRouter(prefix="/v1", tags=["operator"])
_wake_condition_adapter: TypeAdapter[WakeCondition] = TypeAdapter(WakeCondition)


class ProcessSummary(BaseModel):
    id: UUID
    process_type: str
    definition_version: str
    status: str
    state_version: int
    memory_summary: str | None
    open_commitments: tuple[str, ...]
    pending_reviews: int
    updated_at: datetime


class TimelineItem(BaseModel):
    id: str
    kind: str
    occurred_at: datetime
    title: str
    status: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ProcessDetail(BaseModel):
    id: UUID
    process_type: str
    definition_version: str
    status: str
    state_version: int
    workflow_id: str
    authoritative_facts: dict[str, Any]
    customer_claims: dict[str, Any]
    fact_provenance: dict[str, dict[str, Any]]
    memory_summary: str | None
    memory_summary_source_event_ids: tuple[UUID, ...]
    open_commitments: tuple[str, ...]
    current_wake_conditions: tuple[WakeCondition, ...]
    created_at: datetime
    updated_at: datetime
    timeline: tuple[TimelineItem, ...]


class PendingReview(BaseModel):
    thread_id: UUID
    process_instance_id: UUID
    process_type: str
    action_request_id: UUID
    action_type: str
    revision: int
    parameters: dict[str, Any]
    rationale: str
    payload_hash: str
    required_role: str | None
    expires_at: datetime | None
    created_at: datetime


class ReviewCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    command_type: ReviewCommandType
    message: str | None = Field(default=None, max_length=10_000)
    expected_payload_hash: str | None = Field(default=None, min_length=32, max_length=128)

    @model_validator(mode="after")
    def require_command_fields(self) -> "ReviewCommandRequest":
        if self.command_type is ReviewCommandType.APPROVE and not self.expected_payload_hash:
            raise ValueError("approval requires the exact expected payload hash")
        if (
            self.command_type
            in {
                ReviewCommandType.REJECT,
                ReviewCommandType.REQUEST_REVISION,
                ReviewCommandType.COMMENT,
            }
            and not self.message
        ):
            raise ValueError(f"{self.command_type.value} requires a message")
        if self.command_type not in {
            ReviewCommandType.APPROVE,
            ReviewCommandType.REJECT,
            ReviewCommandType.REQUEST_REVISION,
            ReviewCommandType.COMMENT,
        }:
            raise ValueError("unsupported operator command")
        return self


class ReviewCommandResponse(BaseModel):
    command_id: UUID
    thread_status: str
    approval_status: str
    action_status: str


def _session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


@router.get("/processes", response_model=list[ProcessSummary])
async def list_processes(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_process_reader)],
    process_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ProcessSummary]:
    pending_reviews = (
        select(func.count(ReviewThread.id))
        .where(
            ReviewThread.process_instance_id == ProcessInstance.id,
            ReviewThread.status == "open",
        )
        .correlate(ProcessInstance)
        .scalar_subquery()
    )
    query = select(ProcessInstance, pending_reviews.label("pending_reviews")).order_by(
        ProcessInstance.updated_at.desc(), ProcessInstance.id
    )
    if process_status is not None:
        query = query.where(ProcessInstance.status == process_status)
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        rows = (await session.execute(query.limit(limit))).all()
    return [
        ProcessSummary(
            id=process.id,
            process_type=process.process_type,
            definition_version=process.definition_version,
            status=process.status,
            state_version=process.state_version,
            memory_summary=process.memory_summary,
            open_commitments=tuple(process.open_commitments),
            pending_reviews=int(pending_count),
            updated_at=process.updated_at,
        )
        for process, pending_count in rows
    ]


@router.get("/processes/{process_instance_id}", response_model=ProcessDetail)
async def get_process(
    process_instance_id: UUID,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_process_reader)],
    timeline_limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> ProcessDetail:
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        process = await session.scalar(
            select(ProcessInstance).where(ProcessInstance.id == process_instance_id)
        )
        if process is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="process not found")
        timeline = await _load_timeline(session, process_instance_id, limit=timeline_limit)
        return ProcessDetail(
            id=process.id,
            process_type=process.process_type,
            definition_version=process.definition_version,
            status=process.status,
            state_version=process.state_version,
            workflow_id=process.workflow_id,
            authoritative_facts=process.authoritative_facts,
            customer_claims=process.customer_claims,
            fact_provenance=process.fact_provenance,
            memory_summary=process.memory_summary,
            memory_summary_source_event_ids=tuple(
                UUID(value) for value in process.memory_summary_source_event_ids
            ),
            open_commitments=tuple(process.open_commitments),
            current_wake_conditions=tuple(
                _wake_condition_adapter.validate_python(value)
                for value in process.current_wake_conditions
            ),
            created_at=process.created_at,
            updated_at=process.updated_at,
            timeline=tuple(timeline),
        )


@router.get("/reviews", response_model=list[PendingReview])
async def list_pending_reviews(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_review_reader)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PendingReview]:
    query = (
        select(ReviewThread, ApprovalRequest, ActionRequest, ActionRevision, ProcessInstance)
        .join(ApprovalRequest, ApprovalRequest.id == ReviewThread.approval_request_id)
        .join(ActionRequest, ActionRequest.id == ApprovalRequest.action_request_id)
        .join(
            ActionRevision,
            and_(
                ActionRevision.action_request_id == ApprovalRequest.action_request_id,
                ActionRevision.revision == ApprovalRequest.revision,
            ),
        )
        .join(ProcessInstance, ProcessInstance.id == ReviewThread.process_instance_id)
        .where(ReviewThread.status == "open", ApprovalRequest.status == "pending")
        .order_by(ReviewThread.created_at, ReviewThread.id)
        .limit(limit)
    )
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        rows = (await session.execute(query)).all()
    return [
        PendingReview(
            thread_id=thread.id,
            process_instance_id=process.id,
            process_type=process.process_type,
            action_request_id=action.id,
            action_type=action.action_type,
            revision=revision.revision,
            parameters=revision.parameters,
            rationale=revision.rationale,
            payload_hash=approval.payload_hash,
            required_role=approval.required_role,
            expires_at=approval.expires_at,
            created_at=thread.created_at,
        )
        for thread, approval, action, revision, process in rows
    ]


@router.post(
    "/reviews/{review_thread_id}/commands",
    response_model=ReviewCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_review_command(
    review_thread_id: UUID,
    body: ReviewCommandRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> ReviewCommandResponse:
    async with _session_factory(request).begin() as session:
        await set_tenant_context(session, identity.tenant_id)
        target = (
            await session.execute(
                select(ReviewThread, ApprovalRequest)
                .join(ApprovalRequest, ApprovalRequest.id == ReviewThread.approval_request_id)
                .where(ReviewThread.id == review_thread_id)
            )
        ).one_or_none()
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="review thread not found"
            )
        thread, approval = target
        required_scope = (
            CredentialScope.REVIEWS_COMMENT
            if body.command_type is ReviewCommandType.COMMENT
            else CredentialScope.REVIEWS_DECIDE
        )
        identity.require_scope(required_scope)
        if (
            body.command_type is ReviewCommandType.APPROVE
            and approval.required_role is not None
            and not identity.has_role(approval.required_role)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"approval requires role: {approval.required_role}",
            )
        command = ReviewCommand(
            command_id=body.command_id,
            tenant_id=identity.tenant_id,
            process_instance_id=thread.process_instance_id,
            review_thread_id=thread.id,
            action_request_id=approval.action_request_id,
            proposal_revision=approval.revision,
            command_type=body.command_type,
            actor_id=identity.actor_id,
            message=body.message,
            expected_payload_hash=body.expected_payload_hash,
        )
        try:
            result = await ReviewService().apply(session, command)
        except ReviewConflict as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return ReviewCommandResponse(
        command_id=UUID(result.command_id),
        thread_status=result.thread_status,
        approval_status=result.approval_status,
        action_status=result.action_status,
    )


async def _load_timeline(
    session: AsyncSession, process_instance_id: UUID, *, limit: int
) -> list[TimelineItem]:
    items: list[TimelineItem] = []
    events = (
        await session.scalars(
            select(EventInbox)
            .where(EventInbox.process_instance_id == process_instance_id)
            .order_by(EventInbox.received_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(event.id),
            kind="event",
            occurred_at=event.received_at,
            title=event.event_type,
            status=event.correlation_status,
            detail={"source": event.source, "facts": event.event_data.get("facts", [])},
        )
        for event in events
    )
    revisions = (
        await session.scalars(
            select(ProcessStateRevision)
            .where(ProcessStateRevision.process_instance_id == process_instance_id)
            .order_by(ProcessStateRevision.created_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(revision.id),
            kind="decision",
            occurred_at=revision.created_at,
            title=f"Agent decision · version {revision.version}",
            status=revision.process_status,
            detail={
                "decision_status": revision.decision_status,
                "wake_conditions": revision.wake_conditions,
                "open_commitments": revision.open_commitments,
            },
        )
        for revision in revisions
    )
    action_rows = (
        await session.execute(
            select(ActionRequest, ActionRevision)
            .join(
                ActionRevision,
                and_(
                    ActionRevision.action_request_id == ActionRequest.id,
                    ActionRevision.revision == ActionRequest.current_revision,
                ),
            )
            .where(ActionRequest.process_instance_id == process_instance_id)
            .order_by(ActionRequest.created_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(action.id),
            kind="action",
            occurred_at=action.created_at,
            title=action.action_type,
            status=action.status,
            detail={
                "revision": revision.revision,
                "parameters": revision.parameters,
                "rationale": revision.rationale,
            },
        )
        for action, revision in action_rows
    )
    attempts = (
        await session.scalars(
            select(ActionAttempt)
            .where(ActionAttempt.process_instance_id == process_instance_id)
            .order_by(ActionAttempt.started_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(attempt.id),
            kind="attempt",
            occurred_at=attempt.started_at,
            title=f"Provider attempt · {attempt.adapter_id}",
            status=attempt.status,
            detail={
                "provider_reference": attempt.provider_reference,
                "result": attempt.result,
                "error": attempt.error,
            },
        )
        for attempt in attempts
    )
    messages = (
        await session.scalars(
            select(ReviewMessage)
            .where(ReviewMessage.process_instance_id == process_instance_id)
            .order_by(ReviewMessage.created_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(message.id),
            kind="review",
            occurred_at=message.created_at,
            title=message.message_type.replace("_", " ").title(),
            status=None,
            detail={
                "actor_id": str(message.actor_id),
                "message": message.content,
                "proposal_revision": message.proposal_revision,
            },
        )
        for message in messages
    )
    return sorted(items, key=lambda item: (item.occurred_at, item.kind, item.id))[-limit:]
