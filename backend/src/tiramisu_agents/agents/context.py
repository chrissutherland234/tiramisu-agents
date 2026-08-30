"""Load a bounded agent-turn context from authoritative application records."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.processes import (
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
)
from tiramisu_agents.db.models.events import EventInbox
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.processes.definitions import ProcessDefinition


class AgentContextError(ValueError):
    """Raised when persisted state cannot form the requested bounded turn."""


class PostgresAgentContextLoader:
    def __init__(self, *, max_events_per_turn: int = 50) -> None:
        if max_events_per_turn < 1:
            raise ValueError("max_events_per_turn must be positive")
        self._max_events_per_turn = max_events_per_turn

    async def load(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        turn_id: UUID,
        event_ids: tuple[UUID, ...],
        definition: ProcessDefinition,
    ) -> AgentTurnInput:
        if not event_ids:
            raise AgentContextError("an agent turn requires at least one event")
        if len(event_ids) > self._max_events_per_turn:
            raise AgentContextError("agent turn exceeds the event context limit")
        if len(event_ids) != len(set(event_ids)):
            raise AgentContextError("agent turn event IDs must be unique")

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
            instructions=definition.compile_instructions(),
        )
