"""Temporal Activity boundary for durable process state projection."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.processes.state import ProcessStateConflict, ProcessStateService


@dataclass(frozen=True)
class PersistProcessStateCommand:
    tenant_id: str
    process_instance_id: str
    process_definition_id: str
    process_definition_version: str
    agent_turn_id: str
    event_ids: tuple[str, ...]
    workflow_now: datetime
    decision_json: str
    review_command_ids: tuple[str, ...] = ()
    action_attempt_ids: tuple[str, ...] = ()
    timer_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersistProcessStateResult:
    version: int
    status: str


class ProcessStateActivities:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProcessDefinitionRegistry,
        *,
        service: ProcessStateService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._service = service or ProcessStateService()

    @activity.defn(name="persist_process_state")
    async def persist_process_state(
        self, command: PersistProcessStateCommand
    ) -> PersistProcessStateResult:
        definition = self._registry.get(
            command.process_definition_id, command.process_definition_version
        )
        decision = AgentDecision.model_validate_json(command.decision_json)
        try:
            validate_decision(
                decision,
                definition.decision_policy(),
                workflow_now=command.workflow_now,
                expected_event_ids=frozenset(UUID(value) for value in command.event_ids),
                expected_review_command_ids=frozenset(
                    UUID(value) for value in command.review_command_ids
                ),
                expected_action_attempt_ids=frozenset(
                    UUID(value) for value in command.action_attempt_ids
                ),
                expected_timer_ids=frozenset(command.timer_ids),
            )
            async with self._session_factory.begin() as session:
                applied = await self._service.apply_decision(
                    session,
                    tenant_id=UUID(command.tenant_id),
                    process_instance_id=UUID(command.process_instance_id),
                    agent_turn_id=UUID(command.agent_turn_id),
                    decision=decision,
                )
        except (DecisionRejected, ProcessStateConflict) as error:
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error
        return PersistProcessStateResult(
            version=applied.version,
            status=applied.status.value,
        )
