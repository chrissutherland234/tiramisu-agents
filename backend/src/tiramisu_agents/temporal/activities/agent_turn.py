"""Temporal Activity boundary for nondeterministic agent execution."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.agents.context import PostgresAgentContextLoader
from tiramisu_agents.agents.runner import AgentTurnRunner
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.processes.compatibility import (
    DeploymentCompatibility,
    DeploymentCompatibilityError,
)
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.security.tenancy import (
    TenantNotAuthorized,
    TenantSuspended,
    TenantUnavailable,
    require_active_tenant,
    require_authorized_tenant,
)


@dataclass(frozen=True)
class AgentTurnCommand:
    tenant_id: str
    process_instance_id: str
    process_definition_id: str
    process_definition_version: str
    turn_id: str
    event_ids: tuple[str, ...]
    workflow_now: datetime
    review_command_ids: tuple[str, ...] = ()
    action_attempt_ids: tuple[str, ...] = ()
    timer_ids: tuple[str, ...] = ()


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
        compatibility: DeploymentCompatibility,
        context_loader: PostgresAgentContextLoader | None = None,
        event_observer: Callable[[CanonicalEvent, dict[str, Any]], None] | None = None,
        authorized_tenant_ids: frozenset[UUID] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._runner = runner
        self._compatibility = compatibility
        self._context_loader = context_loader or PostgresAgentContextLoader()
        self._event_observer = event_observer
        self._authorized_tenant_ids = authorized_tenant_ids

    @activity.defn(name="run_agent_turn")
    async def run_agent_turn(self, command: AgentTurnCommand) -> AgentTurnActivityResult:
        tenant_id = UUID(command.tenant_id)
        try:
            require_authorized_tenant(tenant_id, self._authorized_tenant_ids)
        except TenantNotAuthorized as error:
            raise ApplicationError(
                "worker deployment is not authorized for this tenant",
                type="TenantNotAuthorized",
                non_retryable=True,
            ) from error
        try:
            definition = self._registry.get(
                command.process_definition_id, command.process_definition_version
            )
        except LookupError as error:
            raise ApplicationError(
                "process definition is not present in the deployed client pack",
                type="DeploymentCompatibilityError",
                non_retryable=True,
            ) from error
        try:
            async with self._session_factory.begin() as session:
                await require_active_tenant(session, tenant_id)
                turn_input = await self._context_loader.load(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=UUID(command.process_instance_id),
                    turn_id=UUID(command.turn_id),
                    event_ids=tuple(UUID(event_id) for event_id in command.event_ids),
                    review_command_ids=tuple(
                        UUID(command_id) for command_id in command.review_command_ids
                    ),
                    action_attempt_ids=tuple(
                        UUID(attempt_id) for attempt_id in command.action_attempt_ids
                    ),
                    timer_ids=command.timer_ids,
                    definition=definition,
                    compatibility=self._compatibility,
                )
            # Recheck as close as possible to the nondeterministic model call.
            async with self._session_factory.begin() as session:
                await require_active_tenant(session, tenant_id)
                process = await session.scalar(
                    select(ProcessInstance).where(
                        ProcessInstance.id == UUID(command.process_instance_id)
                    )
                )
                if process is None:
                    raise DeploymentCompatibilityError("process instance is unavailable")
                self._compatibility.require_process(
                    process_type=process.process_type,
                    definition_version=process.definition_version,
                    client_pack_fingerprint=process.client_pack_fingerprint,
                    extension_manifest_hash=process.extension_manifest_hash,
                    process_definition_fingerprint=process.process_definition_fingerprint,
                )
            if self._event_observer is not None:
                for event in turn_input.events:
                    self._event_observer(event, turn_input.process.authoritative_facts)
        except DeploymentCompatibilityError as error:
            raise ApplicationError(
                str(error),
                type="DeploymentCompatibilityError",
                non_retryable=True,
            ) from error
        except (TenantUnavailable, TenantSuspended) as error:
            raise ApplicationError(
                "tenant safety control blocks agent execution",
                type=type(error).__name__,
                non_retryable=True,
            ) from error
        decision = await self._runner.run_turn(turn_input)
        try:
            validated = validate_decision(
                decision,
                definition.decision_policy(),
                workflow_now=command.workflow_now,
                expected_event_ids=frozenset(event.event_id for event in turn_input.events),
                expected_review_command_ids=frozenset(
                    review.command_id for review in turn_input.reviews
                ),
                expected_action_attempt_ids=frozenset(
                    action_result.attempt_id for action_result in turn_input.action_results
                ),
                expected_timer_ids=frozenset(turn_input.timer_ids),
            )
        except DecisionRejected as error:
            raise ApplicationError(
                str(error), type="DecisionRejected", non_retryable=True
            ) from error
        return AgentTurnActivityResult(decision_json=validated.model_dump_json())
