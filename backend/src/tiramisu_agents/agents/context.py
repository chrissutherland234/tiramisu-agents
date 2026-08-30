"""Load a bounded agent-turn context from authoritative application records."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.processes import (
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
    ReviewTurnContext,
)
from tiramisu_agents.db.models.actions import ActionRequest, ActionRevision, ApprovalRequest
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ReviewMessage, ReviewThread
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.processes.definitions import ProcessDefinition


class AgentContextError(ValueError):
    """Raised when persisted state cannot form the requested bounded turn."""


class PostgresAgentContextLoader:
    def __init__(self, *, max_events_per_turn: int = 50, max_reviews_per_turn: int = 20) -> None:
        if max_events_per_turn < 1:
            raise ValueError("max_events_per_turn must be positive")
        self._max_events_per_turn = max_events_per_turn
        if max_reviews_per_turn < 1:
            raise ValueError("max_reviews_per_turn must be positive")
        self._max_reviews_per_turn = max_reviews_per_turn

    async def load(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        turn_id: UUID,
        event_ids: tuple[UUID, ...],
        review_command_ids: tuple[UUID, ...] = (),
        timer_ids: tuple[str, ...] = (),
        definition: ProcessDefinition,
    ) -> AgentTurnInput:
        if not event_ids and not review_command_ids and not timer_ids:
            raise AgentContextError("an agent turn requires at least one wake source")
        if len(event_ids) > self._max_events_per_turn:
            raise AgentContextError("agent turn exceeds the event context limit")
        if len(event_ids) != len(set(event_ids)):
            raise AgentContextError("agent turn event IDs must be unique")
        if len(review_command_ids) > self._max_reviews_per_turn:
            raise AgentContextError("agent turn exceeds the review context limit")
        if len(review_command_ids) != len(set(review_command_ids)):
            raise AgentContextError("agent turn review command IDs must be unique")
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
            raise AgentContextError("process instance definition does not match the registry")

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
        return AgentTurnInput(
            turn_id=turn_id,
            process=ProcessSnapshot(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                process_type=process.process_type,
                process_definition_version=process.definition_version,
                status=ProcessStatus(process.status),
            ),
            events=events,
            reviews=reviews,
            timer_ids=timer_ids,
            instructions=definition.compile_instructions(),
        )
