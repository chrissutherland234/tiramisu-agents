"""Temporal Activity boundary for provider action execution."""

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.actions.execution import ActionExecutionRejected, ActionExecutor
from tiramisu_agents.processes.compatibility import DeploymentCompatibilityError
from tiramisu_agents.security.tenancy import TenantNotAuthorized, require_authorized_tenant


@dataclass(frozen=True)
class ExecuteActionCommand:
    tenant_id: str
    process_instance_id: str
    action_request_id: str
    revision: int


@dataclass(frozen=True)
class ExecuteActionResult:
    result_json: str


class ActionExecutionActivities:
    def __init__(
        self,
        executor: ActionExecutor,
        *,
        authorized_tenant_ids: frozenset[UUID] | None = None,
    ) -> None:
        self._executor = executor
        self._authorized_tenant_ids = authorized_tenant_ids

    @activity.defn(name="execute_action")
    async def execute_action(self, command: ExecuteActionCommand) -> ExecuteActionResult:
        return await self._run(command, reconcile=False)

    @activity.defn(name="reconcile_action")
    async def reconcile_action(self, command: ExecuteActionCommand) -> ExecuteActionResult:
        return await self._run(command, reconcile=True)

    async def _run(self, command: ExecuteActionCommand, *, reconcile: bool) -> ExecuteActionResult:
        tenant_id = UUID(command.tenant_id)
        try:
            require_authorized_tenant(tenant_id, self._authorized_tenant_ids)
            operation = self._executor.reconcile if reconcile else self._executor.execute
            result = await operation(
                tenant_id=tenant_id,
                process_instance_id=UUID(command.process_instance_id),
                action_request_id=UUID(command.action_request_id),
                revision=command.revision,
            )
        except (
            ActionExecutionRejected,
            DeploymentCompatibilityError,
            TenantNotAuthorized,
            LookupError,
        ) as error:
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error
        payload = {
            **asdict(result),
            "action_request_id": str(result.action_request_id),
            "attempt_id": str(result.attempt_id),
            "status": result.status.value,
            "facts": [fact.model_dump(mode="json") for fact in result.facts],
        }
        return ExecuteActionResult(
            result_json=json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
