"""Load a bounded agent-turn context from authoritative application records."""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.decisions import WakeCondition
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.processes import (
    ActionResultContext,
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
    ReviewTurnContext,
)
from tiramisu_agents.core.limits import (
    DEFAULT_PLATFORM_SAFETY_LIMITS,
    PlatformSafetyLimits,
    SafetyLimitExceeded,
    require_action_parameters,
    require_json_bytes,
    require_memory_content,
    require_process_fact_projection,
    require_utf8_bytes,
)
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ReviewMessage, ReviewThread
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.extensions.runtime import DeploymentRelease
from tiramisu_agents.processes.compatibility import (
    DeploymentCompatibility,
    DeploymentCompatibilityError,
)
from tiramisu_agents.processes.definitions import ProcessDefinition

_wake_condition_adapter: TypeAdapter[WakeCondition] = TypeAdapter(WakeCondition)


class AgentContextError(ValueError):
    """Raised when persisted state cannot form the requested bounded turn."""


class AgentContextLimitExceeded(AgentContextError):
    """Raised when a turn exceeds a hard platform safety ceiling."""


class PostgresAgentContextLoader:
    def __init__(
        self,
        *,
        limits: PlatformSafetyLimits = DEFAULT_PLATFORM_SAFETY_LIMITS,
        max_events_per_turn: int | None = None,
        max_reviews_per_turn: int | None = None,
        max_action_results_per_turn: int | None = None,
        max_timers_per_turn: int | None = None,
        max_agent_context_bytes: int | None = None,
    ) -> None:
        if max_events_per_turn is None:
            max_events_per_turn = limits.max_events_per_turn
        if max_reviews_per_turn is None:
            max_reviews_per_turn = limits.max_reviews_per_turn
        if max_action_results_per_turn is None:
            max_action_results_per_turn = limits.max_action_results_per_turn
        if max_timers_per_turn is None:
            max_timers_per_turn = limits.max_timers_per_turn
        if max_agent_context_bytes is None:
            max_agent_context_bytes = limits.max_agent_context_bytes
        if isinstance(max_events_per_turn, bool) or max_events_per_turn < 1:
            raise ValueError("max_events_per_turn must be positive")
        if max_events_per_turn > limits.max_events_per_turn:
            raise ValueError("max_events_per_turn cannot exceed the platform maximum")
        self._max_events_per_turn = max_events_per_turn
        if isinstance(max_reviews_per_turn, bool) or max_reviews_per_turn < 1:
            raise ValueError("max_reviews_per_turn must be positive")
        if max_reviews_per_turn > limits.max_reviews_per_turn:
            raise ValueError("max_reviews_per_turn cannot exceed the platform maximum")
        self._max_reviews_per_turn = max_reviews_per_turn
        if isinstance(max_action_results_per_turn, bool) or max_action_results_per_turn < 1:
            raise ValueError("max_action_results_per_turn must be positive")
        if max_action_results_per_turn > limits.max_action_results_per_turn:
            raise ValueError("max_action_results_per_turn cannot exceed the platform maximum")
        self._max_action_results_per_turn = max_action_results_per_turn
        if isinstance(max_timers_per_turn, bool) or max_timers_per_turn < 1:
            raise ValueError("max_timers_per_turn must be positive")
        if max_timers_per_turn > limits.max_timers_per_turn:
            raise ValueError("max_timers_per_turn cannot exceed the platform maximum")
        self._max_timers_per_turn = max_timers_per_turn
        if isinstance(max_agent_context_bytes, bool) or max_agent_context_bytes < 1:
            raise ValueError("max_agent_context_bytes must be positive")
        if max_agent_context_bytes > limits.max_agent_context_bytes:
            raise ValueError("max_agent_context_bytes cannot exceed the platform maximum")
        self._max_agent_context_bytes = max_agent_context_bytes
        self._limits = limits

    async def load(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        turn_id: UUID,
        event_ids: tuple[UUID, ...],
        review_command_ids: tuple[UUID, ...] = (),
        action_attempt_ids: tuple[UUID, ...] = (),
        timer_ids: tuple[str, ...] = (),
        definition: ProcessDefinition,
        compatibility: DeploymentCompatibility,
        deployment_release: DeploymentRelease,
    ) -> AgentTurnInput:
        if not event_ids and not review_command_ids and not action_attempt_ids and not timer_ids:
            raise AgentContextError("an agent turn requires at least one wake source")
        if len(event_ids) > self._max_events_per_turn:
            raise AgentContextLimitExceeded("agent turn exceeds the event context limit")
        if len(event_ids) != len(set(event_ids)):
            raise AgentContextError("agent turn event IDs must be unique")
        if len(review_command_ids) > self._max_reviews_per_turn:
            raise AgentContextLimitExceeded("agent turn exceeds the review context limit")
        if len(review_command_ids) != len(set(review_command_ids)):
            raise AgentContextError("agent turn review command IDs must be unique")
        if len(action_attempt_ids) > self._max_action_results_per_turn:
            raise AgentContextLimitExceeded("agent turn exceeds the action result context limit")
        if len(action_attempt_ids) != len(set(action_attempt_ids)):
            raise AgentContextError("agent turn action attempt IDs must be unique")
        if len(timer_ids) > self._max_timers_per_turn:
            raise AgentContextLimitExceeded("agent turn exceeds the timer context limit")
        if any(not value.strip() for value in timer_ids) or len(timer_ids) != len(set(timer_ids)):
            raise AgentContextError("agent turn timer IDs must be nonblank and unique")

        await set_tenant_context(session, tenant_id)
        process = await session.scalar(
            select(ProcessInstance).where(ProcessInstance.id == process_instance_id)
        )
        if process is None:
            raise AgentContextError("process instance not found")
        if (
            process.process_type != definition.id
            or process.definition_version != definition.version
        ):
            raise DeploymentCompatibilityError(
                "process instance definition identity does not match the workflow command"
            )
        compatibility.require_process(
            process_type=process.process_type,
            definition_version=process.definition_version,
            client_pack_fingerprint=process.client_pack_fingerprint,
            extension_manifest_hash=process.extension_manifest_hash,
            process_definition_fingerprint=process.process_definition_fingerprint,
        )
        deployment_release.require_process(
            deployment_id=process.deployment_id,
            deployment_release_fingerprint=process.deployment_release_fingerprint,
            temporal_task_queue=process.temporal_task_queue,
        )

        stored_events = (
            await session.scalars(
                select(EventInbox).where(
                    EventInbox.id.in_(event_ids),
                    EventInbox.process_instance_id == process_instance_id,
                    EventInbox.correlation_status == "matched",
                )
            )
        ).all()
        by_id = {event.id: event for event in stored_events}
        if set(by_id) != set(event_ids):
            raise AgentContextError("one or more turn events are unavailable or unmatched")

        events = tuple(
            CanonicalEvent.model_validate(by_id[event_id].event_data).model_copy(
                update={"process_instance_id": process_instance_id}
            )
            for event_id in event_ids
        )
        review_rows = (
            await session.execute(
                select(ReviewMessage, ReviewThread, ApprovalRequest, ActionRequest, ActionRevision)
                .join(
                    ReviewThread,
                    ReviewThread.id == ReviewMessage.review_thread_id,
                )
                .join(
                    ApprovalRequest,
                    ApprovalRequest.id == ReviewThread.approval_request_id,
                )
                .join(
                    ActionRequest,
                    ActionRequest.id == ApprovalRequest.action_request_id,
                )
                .join(
                    ActionRevision,
                    (ActionRevision.action_request_id == ApprovalRequest.action_request_id)
                    & (ActionRevision.revision == ApprovalRequest.revision),
                )
                .where(
                    ReviewMessage.id.in_(review_command_ids),
                    ReviewMessage.process_instance_id == process_instance_id,
                )
            )
        ).all()
        reviews_by_id = {row.ReviewMessage.id: row for row in review_rows}
        if set(reviews_by_id) != set(review_command_ids):
            raise AgentContextError("one or more review commands are unavailable")
        reviews = tuple(
            ReviewTurnContext(
                command_id=command_id,
                command_type=row.ReviewMessage.message_type,
                review_thread_id=row.ReviewThread.id,
                action_request_id=row.ActionRequest.id,
                proposal_revision=row.ActionRevision.revision,
                actor_id=row.ReviewMessage.actor_id,
                message=row.ReviewMessage.content,
                action_type=row.ActionRequest.action_type,
                proposal_parameters=row.ActionRevision.parameters,
                proposal_payload_hash=row.ActionRevision.payload_hash,
                proposal_rationale=row.ActionRevision.rationale,
            )
            for command_id in review_command_ids
            for row in (reviews_by_id[command_id],)
        )
        action_rows = (
            await session.execute(
                select(
                    ActionAttempt,
                    ActionRequest,
                    ActionRevision,
                    ActionReconciliationDecision,
                )
                .join(
                    ActionRequest,
                    ActionRequest.id == ActionAttempt.action_request_id,
                )
                .join(
                    ActionRevision,
                    (ActionRevision.action_request_id == ActionAttempt.action_request_id)
                    & (ActionRevision.revision == ActionAttempt.revision),
                )
                .outerjoin(
                    ActionReconciliationDecision,
                    (ActionReconciliationDecision.action_attempt_id == ActionAttempt.id)
                    & (ActionReconciliationDecision.tenant_id == ActionAttempt.tenant_id)
                    & (
                        ActionReconciliationDecision.process_instance_id
                        == ActionAttempt.process_instance_id
                    ),
                )
                .where(
                    ActionAttempt.id.in_(action_attempt_ids),
                    ActionAttempt.process_instance_id == process_instance_id,
                )
            )
        ).all()
        actions_by_id = {row.ActionAttempt.id: row for row in action_rows}
        if set(actions_by_id) != set(action_attempt_ids):
            raise AgentContextError("one or more action attempts are unavailable")
        action_results = tuple(
            ActionResultContext(
                attempt_id=attempt_id,
                action_request_id=row.ActionRequest.id,
                revision=row.ActionAttempt.revision,
                action_type=row.ActionRequest.action_type,
                parameters=row.ActionRevision.parameters,
                status=row.ActionAttempt.status,
                adapter_id=row.ActionAttempt.adapter_id,
                idempotency_key=row.ActionAttempt.idempotency_key,
                provider_reference=row.ActionAttempt.provider_reference,
                result=row.ActionAttempt.result,
                facts=tuple(
                    FactObservation.model_validate(fact) for fact in row.ActionAttempt.facts
                ),
                error=row.ActionAttempt.error,
                operator_resolution_id=(
                    row.ActionReconciliationDecision.id
                    if row.ActionReconciliationDecision is not None
                    else None
                ),
                operator_actor_id=(
                    row.ActionReconciliationDecision.actor_id
                    if row.ActionReconciliationDecision is not None
                    else None
                ),
                operator_evidence=(
                    row.ActionReconciliationDecision.evidence
                    if row.ActionReconciliationDecision is not None
                    else None
                ),
            )
            for attempt_id in action_attempt_ids
            for row in (actions_by_id[attempt_id],)
        )
        try:
            require_memory_content(
                summary=process.memory_summary,
                open_commitments=process.open_commitments,
                limits=self._limits,
            )
            for review in reviews:
                if review.message is not None:
                    require_utf8_bytes(
                        review.message,
                        label="review message",
                        max_bytes=self._limits.max_review_message_bytes,
                    )
                require_action_parameters(
                    review.proposal_parameters,
                    label="review proposal parameters",
                    limits=self._limits,
                )
            for result in action_results:
                require_action_parameters(
                    result.parameters,
                    label="action result parameters",
                    limits=self._limits,
                )
        except ValueError as error:
            raise AgentContextLimitExceeded(str(error)) from error
        self._require_safe_fact_projection(
            process=process,
            events=events,
            action_results=action_results,
        )
        turn_input = AgentTurnInput(
            turn_id=turn_id,
            process=ProcessSnapshot(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                process_type=process.process_type,
                process_definition_version=process.definition_version,
                status=ProcessStatus(process.status),
                authoritative_facts=process.authoritative_facts,
                customer_claims=process.customer_claims,
                fact_provenance=process.fact_provenance,
                memory_summary=process.memory_summary,
                memory_summary_source_event_ids=tuple(
                    UUID(value) for value in process.memory_summary_source_event_ids
                ),
                memory_summary_source_review_command_ids=tuple(
                    UUID(value) for value in process.memory_summary_source_review_command_ids
                ),
                memory_summary_source_action_attempt_ids=tuple(
                    UUID(value) for value in process.memory_summary_source_action_attempt_ids
                ),
                memory_summary_source_timer_ids=tuple(process.memory_summary_source_timer_ids),
                open_commitments=tuple(process.open_commitments),
                current_wake_conditions=tuple(
                    _wake_condition_adapter.validate_python(value)
                    for value in process.current_wake_conditions
                ),
                state_version=process.state_version,
            ),
            events=events,
            reviews=reviews,
            action_results=action_results,
            timer_ids=timer_ids,
            instructions=definition.compile_instructions(),
        )
        try:
            require_json_bytes(
                turn_input.model_dump(mode="json"),
                label="agent turn context",
                max_bytes=self._max_agent_context_bytes,
            )
        except SafetyLimitExceeded as error:
            raise AgentContextLimitExceeded(str(error)) from error
        except ValueError as error:
            raise AgentContextError(str(error)) from error
        return turn_input

    def _require_safe_fact_projection(
        self,
        *,
        process: ProcessInstance,
        events: tuple[CanonicalEvent, ...],
        action_results: tuple[ActionResultContext, ...],
    ) -> None:
        authoritative = dict(process.authoritative_facts)
        claims = dict(process.customer_claims)
        provenance = dict(process.fact_provenance)
        for source_type, source_id, observations in (
            *(("event", event.event_id, event.facts) for event in events),
            *(("action_attempt", result.attempt_id, result.facts) for result in action_results),
        ):
            for observation in observations:
                target = authoritative if observation.kind is FactKind.AUTHORITATIVE else claims
                target[observation.key] = observation.model_dump(mode="json")["value"]
                provenance[f"{observation.kind.value}:{observation.key}"] = {
                    "kind": observation.kind.value,
                    "source_type": source_type,
                    "source_id": str(source_id),
                }
        try:
            require_process_fact_projection(
                authoritative_facts=authoritative,
                customer_claims=claims,
                fact_provenance=provenance,
                limits=self._limits,
            )
        except SafetyLimitExceeded as error:
            raise AgentContextLimitExceeded(str(error)) from error
        except ValueError as error:
            raise AgentContextError(str(error)) from error
