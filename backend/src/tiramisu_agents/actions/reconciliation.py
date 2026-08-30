"""Evidence-backed resolution of action outcomes providers cannot establish."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.actions.execution import ActionExecutionResult
from tiramisu_agents.core.contracts.actions import (
    ActionAttemptStatus,
    OperatorActionResolution,
)
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionReconciliationDecision,
    ActionRequest,
)
from tiramisu_agents.db.models.events import OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.session import set_tenant_context


class ActionResolutionConflict(ValueError):
    """Raised when an unknown outcome cannot be resolved by the supplied command."""


class ActionReconciliationService:
    """Resolve one unknown attempt while retaining the operator's immutable evidence."""

    async def resolve_unknown(
        self,
        session: AsyncSession,
        command: OperatorActionResolution,
    ) -> ActionExecutionResult:
        await set_tenant_context(session, command.tenant_id)
        existing = await session.scalar(
            select(ActionReconciliationDecision).where(
                ActionReconciliationDecision.tenant_id == command.tenant_id,
                ActionReconciliationDecision.process_instance_id == command.process_instance_id,
                ActionReconciliationDecision.action_attempt_id == command.action_attempt_id,
            )
        )
        if existing is not None:
            self._require_same_decision(existing, command)
            attempt = await session.scalar(
                select(ActionAttempt).where(ActionAttempt.id == command.action_attempt_id)
            )
            if attempt is None:
                raise ActionResolutionConflict("resolved action attempt is unavailable")
            return self._result(attempt)

        row = (
            await session.execute(
                select(ActionAttempt, ActionRequest)
                .join(ActionRequest, ActionRequest.id == ActionAttempt.action_request_id)
                .where(
                    ActionAttempt.tenant_id == command.tenant_id,
                    ActionAttempt.process_instance_id == command.process_instance_id,
                    ActionAttempt.id == command.action_attempt_id,
                )
                .with_for_update(of=(ActionAttempt, ActionRequest))
            )
        ).one_or_none()
        if row is None:
            raise ActionResolutionConflict("action attempt not found")
        attempt, request = row[0], row[1]
        if attempt.status not in {
            ActionAttemptStatus.UNKNOWN.value,
            ActionAttemptStatus.RECONCILING.value,
        }:
            concurrently_stored = await session.scalar(
                select(ActionReconciliationDecision).where(
                    ActionReconciliationDecision.tenant_id == command.tenant_id,
                    ActionReconciliationDecision.process_instance_id
                    == command.process_instance_id,
                    ActionReconciliationDecision.action_attempt_id
                    == command.action_attempt_id,
                )
            )
            if concurrently_stored is not None:
                self._require_same_decision(concurrently_stored, command)
                return self._result(attempt)
            raise ActionResolutionConflict("only an unknown action outcome can be resolved")

        session.add(
            ActionReconciliationDecision(
                id=command.decision_id,
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                action_request_id=attempt.action_request_id,
                action_attempt_id=attempt.id,
                actor_id=command.actor_id,
                previous_status=attempt.status,
                resolution=command.resolution.value,
                evidence=command.evidence,
                provider_reference=command.provider_reference,
                result=command.result,
            )
        )
        attempt.status = command.resolution.value
        if command.provider_reference is not None:
            attempt.provider_reference = command.provider_reference
        if command.result is not None:
            attempt.result = command.result
        attempt.completed_at = datetime.now(UTC)
        request.status = command.resolution.value
        workflow_id = await session.scalar(
            select(ProcessInstance.workflow_id).where(
                ProcessInstance.id == command.process_instance_id
            )
        )
        if workflow_id is None:
            raise ActionResolutionConflict("action process has no workflow identity")
        await session.execute(
            insert(OutboxMessage)
            .values(
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                message_type="temporal.action_resolution",
                destination=workflow_id,
                deduplication_key=f"action-resolution:{command.decision_id}",
                payload={
                    "command_id": str(command.decision_id),
                    "action_request_id": str(attempt.action_request_id),
                    "action_attempt_id": str(attempt.id),
                    "status": command.resolution.value,
                },
            )
            .on_conflict_do_nothing(constraint="uq_outbox_messages_dedup")
        )
        await session.flush()
        return self._result(attempt)

    @staticmethod
    def _require_same_decision(
        stored: ActionReconciliationDecision,
        command: OperatorActionResolution,
    ) -> None:
        if (
            stored.id != command.decision_id
            or stored.actor_id != command.actor_id
            or stored.resolution != command.resolution.value
            or stored.evidence != command.evidence
            or stored.provider_reference != command.provider_reference
            or stored.result != command.result
        ):
            raise ActionResolutionConflict(
                "action attempt already has a different reconciliation decision"
            )

    @staticmethod
    def _result(attempt: ActionAttempt) -> ActionExecutionResult:
        return ActionExecutionResult(
            action_request_id=attempt.action_request_id,
            attempt_id=attempt.id,
            status=ActionAttemptStatus(attempt.status),
            idempotency_key=attempt.idempotency_key,
            provider_reference=attempt.provider_reference,
            result=attempt.result,
            error=attempt.error,
        )
