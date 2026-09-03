"""Temporal Activity boundary for durable process state projection."""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.extensions.runtime import DeploymentRelease
from tiramisu_agents.processes.control import (
    InterventionInput,
    ProcessControlConflict,
    ProcessControlService,
)
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry
from tiramisu_agents.processes.state import ProcessStateConflict, ProcessStateService
from tiramisu_agents.security.tenancy import (
    TenantNotAuthorized,
    TenantSuspended,
    TenantUnavailable,
    require_active_tenant,
    require_authorized_tenant,
    require_tenant_deployment,
)


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
    wake_conditions_json: str
    pending_action_request_ids: tuple[str, ...]
    terminal: bool


@dataclass(frozen=True)
class RecordProcessInterventionCommand:
    intervention_id: str
    tenant_id: str
    process_instance_id: str
    agent_turn_id: str
    kind: str
    error_type: str
    error: str
    event_ids: tuple[str, ...] = ()
    review_command_ids: tuple[str, ...] = ()
    action_attempt_ids: tuple[str, ...] = ()
    timer_ids: tuple[str, ...] = ()


class ProcessStateActivities:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProcessDefinitionRegistry,
        *,
        service: ProcessStateService | None = None,
        deployment_release: DeploymentRelease | None = None,
        authorized_tenant_ids: frozenset[UUID] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._service = service or ProcessStateService()
        self._deployment_release = deployment_release
        self._authorized_tenant_ids = authorized_tenant_ids

    @activity.defn(name="record_process_intervention")
    async def record_process_intervention(self, command: RecordProcessInterventionCommand) -> None:
        tenant_id = UUID(command.tenant_id)
        try:
            require_authorized_tenant(tenant_id, self._authorized_tenant_ids)
            async with self._session_factory.begin() as session:
                if self._deployment_release is not None:
                    await require_tenant_deployment(
                        session,
                        tenant_id,
                        self._deployment_release.deployment_id,
                    )
                await ProcessControlService().record_intervention(
                    session,
                    InterventionInput(
                        intervention_id=UUID(command.intervention_id),
                        tenant_id=tenant_id,
                        process_instance_id=UUID(command.process_instance_id),
                        agent_turn_id=UUID(command.agent_turn_id),
                        kind=command.kind,
                        error_type=command.error_type,
                        error=command.error,
                        event_ids=tuple(UUID(value) for value in command.event_ids),
                        review_command_ids=tuple(
                            UUID(value) for value in command.review_command_ids
                        ),
                        action_attempt_ids=tuple(
                            UUID(value) for value in command.action_attempt_ids
                        ),
                        timer_ids=command.timer_ids,
                    ),
                )
        except (TenantNotAuthorized, TenantUnavailable, ProcessControlConflict) as error:
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error

    @activity.defn(name="persist_process_state")
    async def persist_process_state(
        self, command: PersistProcessStateCommand
    ) -> PersistProcessStateResult:
        tenant_id = UUID(command.tenant_id)
        try:
            require_authorized_tenant(tenant_id, self._authorized_tenant_ids)
        except TenantNotAuthorized as error:
            raise ApplicationError(
                "worker deployment is not authorized for this tenant",
                type="TenantNotAuthorized",
                non_retryable=True,
            ) from error
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
                # ProcessStateService checks completion against the authoritative
                # projection it locks and updates in the transaction below.
                enforce_completion_requirements=False,
            )
            async with self._session_factory.begin() as session:
                if self._deployment_release is not None:
                    await require_active_tenant(
                        session,
                        tenant_id,
                        deployment_id=self._deployment_release.deployment_id,
                    )
                applied = await self._service.apply_decision(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=UUID(command.process_instance_id),
                    agent_turn_id=UUID(command.agent_turn_id),
                    decision=decision,
                    terminal_states=frozenset(definition.terminal_states),
                    completion_requirements=dict(definition.completion_requirements),
                )
        except (
            DecisionRejected,
            ProcessStateConflict,
            TenantNotAuthorized,
            TenantSuspended,
            TenantUnavailable,
        ) as error:
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error
        return PersistProcessStateResult(
            version=applied.version,
            status=applied.status.value,
            wake_conditions_json=json.dumps(
                [wake.model_dump(mode="json") for wake in applied.wake_conditions],
                sort_keys=True,
                separators=(",", ":"),
            ),
            pending_action_request_ids=tuple(
                str(action_id) for action_id in applied.pending_action_request_ids
            ),
            terminal=applied.terminal,
        )
