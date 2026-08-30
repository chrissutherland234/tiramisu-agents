"""Temporal Activity boundary for provider action execution."""

import json
from dataclasses import asdict, dataclass
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from tiramisu_agents.actions.execution import ActionExecutionRejected, ActionExecutor


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
    def __init__(self, executor: ActionExecutor) -> None:
        self._executor = executor

    @activity.defn(name="execute_action")
    async def execute_action(self, command: ExecuteActionCommand) -> ExecuteActionResult:
        return await self._run(command, reconcile=False)

    @activity.defn(name="reconcile_action")
    async def reconcile_action(self, command: ExecuteActionCommand) -> ExecuteActionResult:
        return await self._run(command, reconcile=True)

    async def _run(
        self, command: ExecuteActionCommand, *, reconcile: bool
    ) -> ExecuteActionResult:
        try:
            operation = self._executor.reconcile if reconcile else self._executor.execute
            result = await operation(
                tenant_id=UUID(command.tenant_id),
                process_instance_id=UUID(command.process_instance_id),
                action_request_id=UUID(command.action_request_id),
                revision=command.revision,
            )
        except (ActionExecutionRejected, LookupError) as error:
            raise ApplicationError(
                str(error), type=type(error).__name__, non_retryable=True
            ) from error
        payload = {
            **asdict(result),
            "action_request_id": str(result.action_request_id),
            "attempt_id": str(result.attempt_id),
            "status": result.status.value,
        }
        return ExecuteActionResult(
            result_json=json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
