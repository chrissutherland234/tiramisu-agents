"""Temporal Activity boundary for nondeterministic agent execution."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.agents.context import PostgresAgentContextLoader
from tiramisu_agents.agents.runner import AgentTurnRunner
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry


@dataclass(frozen=True)
class AgentTurnCommand:
    tenant_id: str
    process_instance_id: str
    process_definition_id: str
    process_definition_version: str
    turn_id: str
    event_ids: tuple[str, ...]
    workflow_now: datetime


@dataclass(frozen=True)
class AgentTurnActivityResult:
    decision_json: str


class AgentTurnActivities:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProcessDefinitionRegistry,
        runner: AgentTurnRunner,
        *,
        context_loader: PostgresAgentContextLoader | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._runner = runner
        self._context_loader = context_loader or PostgresAgentContextLoader()

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(self, command: AgentTurnCommand) -> AgentTurnActivityResult:
        definition = self._registry.get(
            command.process_definition_id, command.process_definition_version
        )
        async with self._session_factory.begin() as session:
            turn_input = await self._context_loader.load(
                session,
                tenant_id=UUID(command.tenant_id),
                process_instance_id=UUID(command.process_instance_id),
                turn_id=UUID(command.turn_id),
                event_ids=tuple(UUID(event_id) for event_id in command.event_ids),
                definition=definition,
            )
        decision = await self._runner.run_turn(turn_input)
        try:
            validated = validate_decision(
                decision,
                definition.decision_policy(),
                workflow_now=command.workflow_now,
                expected_event_ids=frozenset(event.event_id for event in turn_input.events),
            )
        except DecisionRejected as error:
            raise ApplicationError(
                str(error), type="DecisionRejected", non_retryable=True
            ) from error
        return AgentTurnActivityResult(decision_json=validated.model_dump_json())
