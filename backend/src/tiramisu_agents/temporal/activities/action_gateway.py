"""Temporal Activity boundary for idempotent action classification and persistence."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.actions.gateway import ActionGateway, ActionPersistenceConflict
from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry


@dataclass(frozen=True)
class PersistActionsCommand:
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
class PersistActionsResult:
    actions_json: str


class ActionGatewayActivities:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProcessDefinitionRegistry,
        *,
        gateway: ActionGateway | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._gateway = gateway or ActionGateway()

    @activity.defn(name="persist_agent_actions")
    async def persist_agent_actions(self, command: PersistActionsCommand) -> PersistActionsResult:
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
                persisted = await self._gateway.persist_decision(
                    session,
                    tenant_id=UUID(command.tenant_id),
                    process_instance_id=UUID(command.process_instance_id),
                    agent_turn_id=UUID(command.agent_turn_id),
                    process_definition_version=definition.version,
                    decision=decision,
                    policy=definition.action_policy(),
                )
        except (ActionPersistenceConflict, DecisionRejected) as error:
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error

        serialized = [
            {
                **asdict(item),
                "action_request_id": str(item.action_request_id),
                "approval_request_id": (
                    str(item.approval_request_id) if item.approval_request_id else None
                ),
                "review_thread_id": str(item.review_thread_id) if item.review_thread_id else None,
                "outcome": item.outcome.value,
                "status": item.status.value,
            }
            for item in persisted
        ]
        return PersistActionsResult(
            actions_json=json.dumps(serialized, sort_keys=True, separators=(",", ":"))
        )
