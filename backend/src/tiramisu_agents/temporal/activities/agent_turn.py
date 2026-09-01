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

from tiramisu_agents.agents.context import AgentContextError, PostgresAgentContextLoader
from tiramisu_agents.agents.runner import AgentTurnRunner, ProposalCorrection
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.extensions.runtime import DeploymentRelease
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

_MAX_SEMANTIC_CORRECTIONS = 2


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
    proposal_attempt_count: int = 1


class AgentTurnActivities:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProcessDefinitionRegistry,
        runner: AgentTurnRunner,
        *,
        compatibility: DeploymentCompatibility,
        deployment_release: DeploymentRelease,
        context_loader: PostgresAgentContextLoader | None = None,
        event_observer: Callable[[CanonicalEvent, dict[str, Any]], None] | None = None,
        authorized_tenant_ids: frozenset[UUID] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._runner = runner
        self._compatibility = compatibility
        self._deployment_release = deployment_release
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
                await require_active_tenant(
                    session,
                    tenant_id,
                    deployment_id=self._deployment_release.deployment_id,
                )
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
                    deployment_release=self._deployment_release,
                )
            decision_policy = definition.decision_policy()
            expected_event_ids = frozenset(event.event_id for event in turn_input.events)
            expected_review_command_ids = frozenset(
                review.command_id for review in turn_input.reviews
            )
            expected_action_attempt_ids = frozenset(
                action_result.attempt_id for action_result in turn_input.action_results
            )
            expected_timer_ids = frozenset(turn_input.timer_ids)
            correction: ProposalCorrection | None = None
            for proposal_attempt_count in range(1, _MAX_SEMANTIC_CORRECTIONS + 2):
                # Recheck safety controls before every nondeterministic model call without
                # rebuilding the trusted turn snapshot used by corrective attempts.
                await self._require_model_execution_allowed(
                    tenant_id=tenant_id,
                    process_instance_id=UUID(command.process_instance_id),
                )
                if proposal_attempt_count == 1 and self._event_observer is not None:
                    for event in turn_input.events:
                        self._event_observer(event, turn_input.process.authoritative_facts)
                decision = await self._runner.run_turn(turn_input, correction=correction)
                try:
                    validated = validate_decision(
                        decision,
                        decision_policy,
                        workflow_now=command.workflow_now,
                        expected_event_ids=expected_event_ids,
                        expected_review_command_ids=expected_review_command_ids,
                        expected_action_attempt_ids=expected_action_attempt_ids,
                        expected_timer_ids=expected_timer_ids,
                    )
                except DecisionRejected as error:
                    if proposal_attempt_count > _MAX_SEMANTIC_CORRECTIONS:
                        raise ApplicationError(
                            str(error), type="DecisionRejected", non_retryable=True
                        ) from error
                    correction = ProposalCorrection(
                        correction_attempt=proposal_attempt_count,
                        rejected_decision=decision,
                        validation_error=str(error),
                    )
                    continue
                return AgentTurnActivityResult(
                    decision_json=validated.model_dump_json(),
                    proposal_attempt_count=proposal_attempt_count,
                )
        except DeploymentCompatibilityError as error:
            raise ApplicationError(
                str(error),
                type="DeploymentCompatibilityError",
                non_retryable=True,
            ) from error
        except AgentContextError as error:
            raise ApplicationError(
                str(error),
                type=type(error).__name__,
                non_retryable=True,
            ) from error
        except (TenantNotAuthorized, TenantUnavailable, TenantSuspended) as error:
            raise ApplicationError(
                "tenant safety control blocks agent execution",
                type=type(error).__name__,
                non_retryable=True,
            ) from error
        raise AssertionError("agent proposal loop exited without a result")

    async def _require_model_execution_allowed(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
    ) -> None:
        async with self._session_factory.begin() as session:
            await require_active_tenant(
                session,
                tenant_id,
                deployment_id=self._deployment_release.deployment_id,
            )
            process = await session.scalar(
                select(ProcessInstance).where(ProcessInstance.id == process_instance_id)
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
            self._deployment_release.require_process(
                deployment_id=process.deployment_id,
                deployment_release_fingerprint=process.deployment_release_fingerprint,
                temporal_task_queue=process.temporal_task_queue,
            )
