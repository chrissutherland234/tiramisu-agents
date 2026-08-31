"""Load a bounded agent-turn context from authoritative application records."""

from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.decisions import WakeCondition
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.core.contracts.processes import (
    ActionResultContext,
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
    ReviewTurnContext,
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


class PostgresAgentContextLoader:
    def __init__(
        self,
        *,
        max_events_per_turn: int = 50,
        max_reviews_per_turn: int = 20,
        max_action_results_per_turn: int = 20,
    ) -> None:
        if max_events_per_turn < 1:
            raise ValueError("max_events_per_turn must be positive")
        self._max_events_per_turn = max_events_per_turn
        if max_reviews_per_turn < 1:
            raise ValueError("max_reviews_per_turn must be positive")
        self._max_reviews_per_turn = max_reviews_per_turn
        if max_action_results_per_turn < 1:
            raise ValueError("max_action_results_per_turn must be positive")
        self._max_action_results_per_turn = max_action_results_per_turn

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
            raise AgentContextError("agent turn exceeds the event context limit")
        if len(event_ids) != len(set(event_ids)):
            raise AgentContextError("agent turn event IDs must be unique")
        if len(review_command_ids) > self._max_reviews_per_turn:
            raise AgentContextError("agent turn exceeds the review context limit")
        if len(review_command_ids) != len(set(review_command_ids)):
            raise AgentContextError("agent turn review command IDs must be unique")
        if len(action_attempt_ids) > self._max_action_results_per_turn:
            raise AgentContextError("agent turn exceeds the action result context limit")
        if len(action_attempt_ids) != len(set(action_attempt_ids)):
            raise AgentContextError("agent turn action attempt IDs must be unique")
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
        return AgentTurnInput(
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
