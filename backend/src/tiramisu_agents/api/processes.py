"""Tenant-scoped development operator views and review commands."""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.operator_auth import (
    OperatorIdentity,
    require_operator_identity,
    require_process_reader,
    require_review_reader,
)
from tiramisu_agents.budgets import ModelBudget
from tiramisu_agents.budgets.breakers import (
    BreakerConflict,
    BreakerScope,
    BreakerState,
    CircuitBreakerService,
)
from tiramisu_agents.budgets.ledger import ModelUsageService
from tiramisu_agents.communications import CommunicationPolicy
from tiramisu_agents.communications.safety import CommunicationSafetyService
from tiramisu_agents.core.contracts.decisions import WakeCondition
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.core.limits import DEFAULT_PLATFORM_SAFETY_LIMITS, require_utf8_bytes
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.breakers import CircuitBreaker
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ReviewMessage, ReviewThread
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.processes.control import (
    ProcessControlConflict,
    ProcessControlInput,
    ProcessControlService,
    ProcessControlType,
)
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
    current_wake_conditions: tuple[WakeCondition, ...]
    pending_reviews: int
    updated_at: datetime


class TimelineItem(BaseModel):
    id: str
    kind: str
    occurred_at: datetime
    title: str
    status: str | None = None
    agent_turn_id: UUID | None = None
    action_request_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ProcessDetail(BaseModel):
    id: UUID
    process_type: str
    definition_version: str
    status: str
    state_version: int
    workflow_id: str
    deployment_id: str
    deployment_release_fingerprint: str
    temporal_task_queue: str
    client_pack_fingerprint: str
    process_definition_fingerprint: str
    authoritative_facts: dict[str, Any]
    customer_claims: dict[str, Any]
    fact_provenance: dict[str, dict[str, Any]]
    memory_summary: str | None
    memory_summary_source_event_ids: tuple[UUID, ...]
    open_commitments: tuple[str, ...]
    current_wake_conditions: tuple[WakeCondition, ...]
    created_at: datetime
    updated_at: datetime
    communication_safety: "CommunicationSafetySummary | None"
    model_budget: "ModelBudgetSummary | None"
    breakers: tuple["BreakerStateSummary", ...]
    interventions: tuple["ProcessInterventionSummary", ...]
    timeline: tuple[TimelineItem, ...]


class ProcessInterventionSummary(BaseModel):
    id: UUID
    agent_turn_id: UUID
    kind: str
    status: str
    error_type: str
    error: str
    source_event_ids: tuple[UUID, ...]
    source_review_command_ids: tuple[UUID, ...]
    source_action_attempt_ids: tuple[UUID, ...]
    source_timer_ids: tuple[str, ...]
    resolved_by_command_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime


class CommunicationBlockSummary(BaseModel):
    code: str
    message: str
    next_allowed_at: datetime | None


class ModelBudgetBlockSummary(BaseModel):
    code: str
    message: str


class ModelBudgetSummary(BaseModel):
    evaluated_at: datetime
    model_allowed_now: bool
    blocks: tuple[ModelBudgetBlockSummary, ...]
    spent_input_tokens: int
    max_input_tokens_per_process: int
    spent_output_tokens: int
    max_output_tokens_per_process: int
    spent_total_tokens: int
    max_total_tokens_per_process: int
    spent_cost_micros: int
    max_cost_micros_per_process: int


class CommunicationSafetySummary(BaseModel):
    evaluated_at: datetime
    outbound_action_types: tuple[str, ...]
    outbound_allowed_now: bool
    blocks: tuple[CommunicationBlockSummary, ...]
    outbound_messages_total: int
    max_outbound_messages_per_process: int
    outbound_messages_in_window: int
    max_outbound_messages_per_window: int
    outbound_message_window_hours: int
    follow_ups_since_reply: int
    max_follow_ups_without_reply: int
    minimum_follow_up_interval_hours: int
    last_human_reply_at: datetime | None
    latest_automated_response_at: datetime | None
    opted_out_at: datetime | None
    process_expires_at: datetime
    quiet_hours_timezone: str | None
    quiet_hours_start_local: str | None
    quiet_hours_end_local: str | None


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

    @field_validator("message")
    @classmethod
    def require_bounded_message(cls, value: str | None) -> str | None:
        if value is not None:
            require_utf8_bytes(
                value,
                label="review message",
                max_bytes=DEFAULT_PLATFORM_SAFETY_LIMITS.max_review_message_bytes,
            )
        return value

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


class ProcessControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    command_type: ProcessControlType
    reason: str = Field(min_length=1, max_length=10_000)
    intervention_id: UUID | None = None

    @field_validator("reason")
    @classmethod
    def require_bounded_reason(cls, value: str) -> str:
        require_utf8_bytes(
            value,
            label="operator guidance",
            max_bytes=DEFAULT_PLATFORM_SAFETY_LIMITS.max_operator_guidance_bytes,
        )
        return value

    @model_validator(mode="after")
    def require_intervention_for_retry(self) -> "ProcessControlRequest":
        if self.command_type is ProcessControlType.RETRY and self.intervention_id is None:
            raise ValueError("retry requires intervention_id")
        if self.command_type is not ProcessControlType.RETRY and self.intervention_id is not None:
            raise ValueError("intervention_id is only valid for retry")
        return self


class ProcessControlResponse(BaseModel):
    command_id: UUID
    command_type: ProcessControlType


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
            current_wake_conditions=tuple(
                _wake_condition_adapter.validate_python(value)
                for value in process.current_wake_conditions
            ),
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
        interventions = (
            await session.scalars(
                select(ProcessIntervention)
                .where(ProcessIntervention.process_instance_id == process_instance_id)
                .order_by(ProcessIntervention.created_at.desc())
            )
        ).all()
        timeline = await _load_timeline(session, process_instance_id, limit=timeline_limit)
        communication_safety = await _communication_safety_summary(
            request,
            session,
            identity.tenant_id,
            process,
        )
        model_budget = await _model_budget_summary(
            request,
            session,
            identity.tenant_id,
            process,
        )
        breakers = await _latest_breaker_summaries(session, identity.tenant_id)
        return ProcessDetail(
            id=process.id,
            process_type=process.process_type,
            definition_version=process.definition_version,
            status=process.status,
            state_version=process.state_version,
            workflow_id=process.workflow_id,
            deployment_id=process.deployment_id,
            deployment_release_fingerprint=process.deployment_release_fingerprint,
            temporal_task_queue=process.temporal_task_queue,
            client_pack_fingerprint=process.client_pack_fingerprint,
            process_definition_fingerprint=process.process_definition_fingerprint,
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
            communication_safety=communication_safety,
            model_budget=model_budget,
            breakers=tuple(breakers),
            interventions=tuple(
                ProcessInterventionSummary(
                    id=item.id,
                    agent_turn_id=item.agent_turn_id,
                    kind=item.kind,
                    status=item.status,
                    error_type=item.error_type,
                    error=item.error,
                    source_event_ids=tuple(UUID(value) for value in item.source_event_ids),
                    source_review_command_ids=tuple(
                        UUID(value) for value in item.source_review_command_ids
                    ),
                    source_action_attempt_ids=tuple(
                        UUID(value) for value in item.source_action_attempt_ids
                    ),
                    source_timer_ids=tuple(item.source_timer_ids),
                    resolved_by_command_id=item.resolved_by_command_id,
                    resolved_at=item.resolved_at,
                    created_at=item.created_at,
                )
                for item in interventions
            ),
            timeline=tuple(timeline),
        )


async def _communication_safety_summary(
    request: Request,
    session: AsyncSession,
    tenant_id: UUID,
    process: ProcessInstance,
) -> CommunicationSafetySummary | None:
    registry = request.app.state.process_registry
    if registry is None:
        return None
    try:
        definition = registry.get(process.process_type, process.definition_version)
    except LookupError:
        return None
    policy = CommunicationPolicy.from_definition(definition)
    if not policy.outbound_action_types:
        return None
    snapshot = await CommunicationSafetyService().inspect(
        session,
        tenant_id=tenant_id,
        process=process,
        policy=policy,
        now=datetime.now(UTC),
    )
    quiet_hours = definition.communications.quiet_hours
    return CommunicationSafetySummary(
        evaluated_at=snapshot.evaluated_at,
        outbound_action_types=tuple(sorted(policy.outbound_action_types)),
        outbound_allowed_now=snapshot.outbound_allowed_now,
        blocks=tuple(
            CommunicationBlockSummary(
                code=block.code.value,
                message=block.message,
                next_allowed_at=block.next_allowed_at,
            )
            for block in snapshot.blocks
        ),
        outbound_messages_total=snapshot.outbound_messages_total,
        max_outbound_messages_per_process=policy.max_outbound_messages_per_process,
        outbound_messages_in_window=snapshot.outbound_messages_in_window,
        max_outbound_messages_per_window=policy.max_outbound_messages_per_window,
        outbound_message_window_hours=int(policy.outbound_message_window.total_seconds() // 3600),
        follow_ups_since_reply=snapshot.follow_ups_since_reply,
        max_follow_ups_without_reply=policy.max_follow_ups_without_reply,
        minimum_follow_up_interval_hours=int(
            policy.minimum_follow_up_interval.total_seconds() // 3600
        ),
        last_human_reply_at=snapshot.last_human_reply_at,
        latest_automated_response_at=snapshot.latest_automated_response_at,
        opted_out_at=snapshot.opted_out_at,
        process_expires_at=snapshot.process_expires_at,
        quiet_hours_timezone=quiet_hours.timezone if quiet_hours else None,
        quiet_hours_start_local=(
            quiet_hours.start_local.isoformat(timespec="minutes") if quiet_hours else None
        ),
        quiet_hours_end_local=(
            quiet_hours.end_local.isoformat(timespec="minutes") if quiet_hours else None
        ),
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
        process = await session.scalar(
            select(ProcessInstance).where(ProcessInstance.id == thread.process_instance_id)
        )
        if process is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="process not found")
        registry = request.app.state.process_registry
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="process definition registry is unavailable",
            )
        definition = registry.get(process.process_type, process.definition_version)
        if body.command_type not in definition.review.commands:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="review command is not enabled by the process definition",
            )
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


@router.post(
    "/processes/{process_instance_id}/controls",
    response_model=ProcessControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_process_control(
    process_instance_id: UUID,
    body: ProcessControlRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> ProcessControlResponse:
    identity.require_scope(CredentialScope.PROCESSES_CONTROL)
    try:
        async with _session_factory(request).begin() as session:
            stored = await ProcessControlService().apply_control(
                session,
                ProcessControlInput(
                    command_id=body.command_id,
                    tenant_id=identity.tenant_id,
                    process_instance_id=process_instance_id,
                    actor_id=identity.actor_id,
                    command_type=body.command_type,
                    reason=body.reason,
                    intervention_id=body.intervention_id,
                ),
            )
    except ProcessControlConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return ProcessControlResponse(
        command_id=stored.id,
        command_type=ProcessControlType(stored.command_type),
    )


class BreakerTransitionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str
    target: str = ""
    reason: str = Field(min_length=1, max_length=10_000)


class BreakerStateSummary(BaseModel):
    scope: str
    target: str
    tripped: bool
    reason: str
    actor_id: UUID
    transitioned_at: datetime


def _summarize_breaker(state: BreakerState) -> BreakerStateSummary:
    return BreakerStateSummary(
        scope=state.scope.value,
        target=state.target,
        tripped=state.tripped,
        reason=state.reason,
        actor_id=state.actor_id,
        transitioned_at=state.transitioned_at,
    )


@router.get("/breakers", response_model=list[BreakerStateSummary])
async def list_breakers(
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_process_reader)],
) -> list[BreakerStateSummary]:
    async with _session_factory(request).begin() as session:
        return await _latest_breaker_summaries(session, identity.tenant_id)


async def _latest_breaker_summaries(
    session: AsyncSession, tenant_id: UUID
) -> list[BreakerStateSummary]:
    await set_tenant_context(session, tenant_id)
    rows = (
        await session.scalars(
            select(CircuitBreaker)
            .where(CircuitBreaker.tenant_id == tenant_id)
            .order_by(CircuitBreaker.created_at.desc(), CircuitBreaker.id.desc())
        )
    ).all()
    latest: dict[tuple[str, str], BreakerStateSummary] = {}
    for row in rows:
        key = (row.scope, row.target)
        if key not in latest:
            latest[key] = BreakerStateSummary(
                scope=row.scope,
                target=row.target,
                tripped=row.tripped,
                reason=row.reason,
                actor_id=row.actor_id,
                transitioned_at=row.created_at,
            )
    return [latest[key] for key in sorted(latest)]


async def _model_budget_summary(
    request: Request,
    session: AsyncSession,
    tenant_id: UUID,
    process: ProcessInstance,
) -> ModelBudgetSummary | None:
    registry = request.app.state.process_registry
    if registry is None:
        return None
    try:
        definition = registry.get(process.process_type, process.definition_version)
    except LookupError:
        return None
    budget = ModelBudget.from_definition(definition)
    snapshot = await ModelUsageService().inspect(
        session,
        tenant_id=tenant_id,
        process_instance_id=process.id,
        budget=budget,
    )
    return ModelBudgetSummary(
        evaluated_at=datetime.now(UTC),
        model_allowed_now=snapshot.model_allowed_now,
        blocks=tuple(
            ModelBudgetBlockSummary(code=block.code.value, message=block.message)
            for block in snapshot.blocks
        ),
        spent_input_tokens=snapshot.evaluated_spent.input_tokens,
        max_input_tokens_per_process=budget.max_input_tokens_per_process,
        spent_output_tokens=snapshot.evaluated_spent.output_tokens,
        max_output_tokens_per_process=budget.max_output_tokens_per_process,
        spent_total_tokens=snapshot.evaluated_spent.total_tokens,
        max_total_tokens_per_process=budget.max_total_tokens_per_process,
        spent_cost_micros=snapshot.evaluated_cost_micros,
        max_cost_micros_per_process=budget.max_cost_micros_per_process,
    )


@router.post(
    "/breakers/trip",
    response_model=BreakerStateSummary,
    status_code=status.HTTP_200_OK,
)
async def trip_breaker(
    body: BreakerTransitionRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> BreakerStateSummary:
    return _summarize_breaker(
        await _apply_breaker_transition(body, request, identity, tripped=True)
    )


@router.post(
    "/breakers/reset",
    response_model=BreakerStateSummary,
    status_code=status.HTTP_200_OK,
)
async def reset_breaker(
    body: BreakerTransitionRequest,
    request: Request,
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> BreakerStateSummary:
    return _summarize_breaker(
        await _apply_breaker_transition(body, request, identity, tripped=False)
    )


async def _apply_breaker_transition(
    body: BreakerTransitionRequest,
    request: Request,
    identity: OperatorIdentity,
    *,
    tripped: bool,
) -> BreakerState:
    identity.require_scope(CredentialScope.PROCESSES_CONTROL)
    try:
        scope = BreakerScope(body.scope)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown breaker scope: {body.scope}",
        ) from error
    try:
        async with _session_factory(request).begin() as session:
            service = CircuitBreakerService()
            if tripped:
                return await service.trip(
                    session,
                    tenant_id=identity.tenant_id,
                    scope=scope,
                    target=body.target,
                    actor_id=identity.actor_id,
                    reason=body.reason,
                )
            return await service.reset(
                session,
                tenant_id=identity.tenant_id,
                scope=scope,
                target=body.target,
                actor_id=identity.actor_id,
                reason=body.reason,
            )
    except BreakerConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error


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
            agent_turn_id=revision.agent_turn_id,
            detail={
                "decision_status": revision.decision_status,
                "memory_summary": revision.memory_summary,
                "wake_conditions": revision.wake_conditions,
                "open_commitments": revision.open_commitments,
            },
        )
        for revision in revisions
    )
    action_rows = (
        await session.execute(
            select(ActionRequest, ActionRevision, ProcessStateRevision)
            .join(
                ActionRevision,
                and_(
                    ActionRevision.action_request_id == ActionRequest.id,
                    ActionRevision.revision == ActionRequest.current_revision,
                ),
            )
            .outerjoin(
                ProcessStateRevision,
                and_(
                    ProcessStateRevision.process_instance_id == ActionRequest.process_instance_id,
                    ProcessStateRevision.agent_turn_id == ActionRequest.agent_turn_id,
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
            occurred_at=(
                turn_revision.created_at if turn_revision is not None else action.created_at
            ),
            title=action.action_type,
            status=action.status,
            agent_turn_id=action.agent_turn_id,
            action_request_id=action.id,
            detail={
                "revision": revision.revision,
                "proposed_at": action.created_at,
                "parameters": revision.parameters,
                "rationale": revision.rationale,
                "supersedes_action_request_id": (
                    str(action.supersedes_action_request_id)
                    if action.supersedes_action_request_id is not None
                    else None
                ),
            },
        )
        for action, revision, turn_revision in action_rows
    )
    attempt_rows = (
        await session.execute(
            select(ActionAttempt, ActionRequest)
            .join(ActionRequest, ActionRequest.id == ActionAttempt.action_request_id)
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
            agent_turn_id=action.agent_turn_id,
            action_request_id=attempt.action_request_id,
            detail={
                "provider_reference": attempt.provider_reference,
                "result": attempt.result,
                "conflict": attempt.conflict,
                "facts": attempt.facts,
                "error": attempt.error,
            },
        )
        for attempt, action in attempt_rows
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
    interventions = (
        await session.scalars(
            select(ProcessIntervention)
            .where(ProcessIntervention.process_instance_id == process_instance_id)
            .order_by(ProcessIntervention.created_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(intervention.id),
            kind="intervention",
            occurred_at=intervention.created_at,
            title=intervention.error_type,
            status=intervention.status,
            detail={
                "kind": intervention.kind,
                "error": intervention.error,
                "agent_turn_id": str(intervention.agent_turn_id),
                "resolved_by_command_id": (
                    str(intervention.resolved_by_command_id)
                    if intervention.resolved_by_command_id is not None
                    else None
                ),
            },
        )
        for intervention in interventions
    )
    controls = (
        await session.scalars(
            select(ProcessControlCommand)
            .where(ProcessControlCommand.process_instance_id == process_instance_id)
            .order_by(ProcessControlCommand.created_at.desc())
            .limit(limit)
        )
    ).all()
    items.extend(
        TimelineItem(
            id=str(control.id),
            kind="control",
            occurred_at=control.created_at,
            title=control.command_type.replace("_", " ").title(),
            status=None,
            detail={
                "actor_id": str(control.actor_id),
                "reason": control.reason,
                "intervention_id": (
                    str(control.intervention_id) if control.intervention_id is not None else None
                ),
            },
        )
        for control in controls
    )
    return sorted(
        items,
        key=lambda item: (
            item.occurred_at,
            1 if item.kind == "action" else 0,
            item.kind,
            item.id,
        ),
    )[-limit:]
