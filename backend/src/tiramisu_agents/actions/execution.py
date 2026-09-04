"""Approval-aware, idempotent provider action execution."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.adapters.registry import ActionAdapterRegistry
from tiramisu_agents.core.action_identity import execution_idempotency_key
from tiramisu_agents.core.contracts.actions import (
    ActionAttemptStatus,
    ActionConflict,
    ActionRequestStatus,
    ApprovalStatus,
    PermissionOutcome,
)
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.core.ports.actions import (
    AmbiguousActionOutcome,
    DefinitiveActionConflict,
    DefinitiveActionFailure,
    ProviderActionRequest,
    ProviderActionResult,
)
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ApprovalDecision
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.extensions.runtime import DeploymentRelease
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.security.tenancy import (
    TenantNotAuthorized,
    TenantSuspended,
    TenantUnavailable,
    require_active_tenant,
    require_tenant_deployment,
)


class ActionExecutionRejected(ValueError):
    """Raised when current durable state does not authorize execution."""


class ActionExecutionSuspended(ActionExecutionRejected):
    """Raised when an otherwise recoverable execution is blocked by tenant suspension."""


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    action_request_id: UUID
    attempt_id: UUID
    status: ActionAttemptStatus
    idempotency_key: str
    provider_reference: str | None
    result: dict[str, object] | None
    facts: tuple[FactObservation, ...]
    error: str | None
    conflict: ActionConflict | None = None


class ActionExecutor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapters: ActionAdapterRegistry,
        compatibility: DeploymentCompatibility,
        deployment_release: DeploymentRelease,
    ) -> None:
        self._session_factory = session_factory
        self._adapters = adapters
        self._compatibility = compatibility
        self._deployment_release = deployment_release

    async def execute(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        action_request_id: UUID,
        revision: int,
    ) -> ActionExecutionResult:
        async with self._session_factory.begin() as session:
            await self._require_execution_enabled(session, tenant_id)
            request, action_revision, _, process = await self._load_authorized_action(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                action_request_id=action_request_id,
                revision=revision,
            )
            self._require_compatible_process(process)
            adapter = self._adapters.resolve(request.action_type)
            key = execution_idempotency_key(
                tenant_id,
                process_instance_id,
                action_request_id,
                revision,
                action_revision.payload_hash,
            )
            inserted_id = await session.scalar(
                insert(ActionAttempt)
                .values(
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                    action_request_id=action_request_id,
                    revision=revision,
                    attempt_number=1,
                    idempotency_key=key,
                    adapter_id=adapter.id,
                    status=ActionAttemptStatus.EXECUTING.value,
                    started_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(constraint="uq_action_attempt_idempotency")
                .returning(ActionAttempt.id)
            )
            attempt = await session.scalar(
                select(ActionAttempt).where(
                    ActionAttempt.tenant_id == tenant_id,
                    ActionAttempt.idempotency_key == key,
                )
            )
            if attempt is None:
                raise RuntimeError("action attempt was not persisted")
            if attempt.adapter_id != adapter.id:
                raise ActionExecutionRejected("action adapter binding changed for this attempt")
            if attempt.status in {
                ActionAttemptStatus.SUCCEEDED.value,
                ActionAttemptStatus.FAILED.value,
                ActionAttemptStatus.CONFLICT.value,
            }:
                return self._result(attempt)
            request.status = ActionRequestStatus.EXECUTING.value
            is_new_attempt = inserted_id is not None

        if not is_new_attempt:
            try:
                recovered = await adapter.lookup(key)
            except Exception as error:
                return await self._record_unknown(
                    tenant_id,
                    action_request_id,
                    key,
                    f"lookup failed: {type(error).__name__}: {error}",
                )
            if recovered is not None:
                if isinstance(recovered, ActionConflict):
                    return await self._record_conflict(tenant_id, action_request_id, key, recovered)
                return await self._record_success(tenant_id, action_request_id, key, recovered)
            if not adapter.guarantees_idempotency:
                return await self._record_unknown(
                    tenant_id,
                    action_request_id,
                    key,
                    "provider cannot safely retry an unresolved execution",
                )

        provider_result: ProviderActionResult | None = None
        provider_error: Exception | None = None
        async with self._session_factory.begin() as session:
            await self._require_execution_enabled(session, tenant_id)
            request, action_revision, _, process = await self._load_authorized_action(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                action_request_id=action_request_id,
                revision=revision,
            )
            self._require_compatible_process(process)
            provider_request = ProviderActionRequest(
                action_type=request.action_type,
                parameters=action_revision.parameters,
                idempotency_key=key,
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                authoritative_facts=dict(process.authoritative_facts),
            )
            # Hold the process/action row locks across the provider boundary. A
            # concurrent pause or terminal control must serialize before or after
            # the side effect; it cannot claim to have stopped work while this call
            # is crossing the final authorization fence.
            try:
                provider_result = await adapter.execute(provider_request)
            except Exception as error:
                provider_error = error

        if isinstance(provider_error, DefinitiveActionConflict):
            return await self._record_conflict(
                tenant_id,
                action_request_id,
                key,
                provider_error.conflict,
            )
        if isinstance(provider_error, DefinitiveActionFailure):
            error = provider_error
            return await self._record_failure(tenant_id, action_request_id, key, str(error))
        if isinstance(provider_error, AmbiguousActionOutcome):
            error = provider_error
            return await self._record_unknown(tenant_id, action_request_id, key, str(error))
        if provider_error is not None:
            error = provider_error
            return await self._record_unknown(
                tenant_id,
                action_request_id,
                key,
                f"{type(error).__name__}: {error}",
            )
        if provider_result is None:
            raise RuntimeError("provider execution returned neither a result nor an error")
        return await self._record_success(tenant_id, action_request_id, key, provider_result)

    async def _require_execution_enabled(self, session: AsyncSession, tenant_id: UUID) -> None:
        try:
            await require_active_tenant(
                session,
                tenant_id,
                deployment_id=self._deployment_release.deployment_id,
            )
        except TenantSuspended as error:
            raise ActionExecutionSuspended(
                "tenant safety control blocks action execution"
            ) from error
        except TenantUnavailable as error:
            raise ActionExecutionRejected("action execution tenant is unavailable") from error
        except TenantNotAuthorized as error:
            raise ActionExecutionRejected(
                "tenant is not assigned to this action-execution deployment"
            ) from error

    async def reconcile(
        self,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        action_request_id: UUID,
        revision: int,
    ) -> ActionExecutionResult:
        """Use provider lookup only; reconciliation never repeats the side effect."""

        async with self._session_factory.begin() as session:
            await self._require_reconciliation_enabled(session, tenant_id)
            request, action_revision, _, process = await self._load_authorized_action(
                session,
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                action_request_id=action_request_id,
                revision=revision,
            )
            self._require_compatible_process(process)
            adapter = self._adapters.resolve(request.action_type)
            key = execution_idempotency_key(
                tenant_id,
                process_instance_id,
                action_request_id,
                revision,
                action_revision.payload_hash,
            )
            attempt = await session.scalar(
                select(ActionAttempt).where(
                    ActionAttempt.tenant_id == tenant_id,
                    ActionAttempt.idempotency_key == key,
                )
            )
            if attempt is None:
                raise ActionExecutionRejected("action has no execution attempt to reconcile")
            if attempt.status in {
                ActionAttemptStatus.SUCCEEDED.value,
                ActionAttemptStatus.FAILED.value,
                ActionAttemptStatus.CONFLICT.value,
            }:
                return self._result(attempt)
            attempt.status = ActionAttemptStatus.RECONCILING.value
            request.status = ActionRequestStatus.RECONCILING.value

        try:
            recovered = await adapter.lookup(key)
        except Exception as error:
            return await self._record_unknown(
                tenant_id,
                action_request_id,
                key,
                f"reconciliation lookup failed: {type(error).__name__}: {error}",
            )
        if recovered is None:
            return await self._record_unknown(
                tenant_id,
                action_request_id,
                key,
                "provider lookup could not establish the execution outcome",
            )
        if isinstance(recovered, ActionConflict):
            return await self._record_conflict(tenant_id, action_request_id, key, recovered)
        return await self._record_success(tenant_id, action_request_id, key, recovered)

    async def _require_reconciliation_enabled(self, session: AsyncSession, tenant_id: UUID) -> None:
        try:
            await require_tenant_deployment(
                session,
                tenant_id,
                self._deployment_release.deployment_id,
            )
        except TenantUnavailable as error:
            raise ActionExecutionRejected("reconciliation tenant is unavailable") from error
        except TenantNotAuthorized as error:
            raise ActionExecutionRejected(
                "tenant is not assigned to this reconciliation deployment"
            ) from error

    def _require_compatible_process(self, process: ProcessInstance) -> None:
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

    async def _load_authorized_action(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        action_request_id: UUID,
        revision: int,
    ) -> tuple[ActionRequest, ActionRevision, ActionPolicyRecord, ProcessInstance]:
        row = (
            await session.execute(
                select(ActionRequest, ActionRevision, ActionPolicyRecord, ProcessInstance)
                .join(
                    ActionRevision,
                    (ActionRevision.action_request_id == ActionRequest.id)
                    & (ActionRevision.revision == revision),
                )
                .join(
                    ActionPolicyRecord,
                    (ActionPolicyRecord.action_request_id == ActionRequest.id)
                    & (ActionPolicyRecord.revision == revision),
                )
                .join(
                    ProcessInstance,
                    (ProcessInstance.tenant_id == ActionRequest.tenant_id)
                    & (ProcessInstance.id == ActionRequest.process_instance_id),
                )
                .where(
                    ActionRequest.tenant_id == tenant_id,
                    ActionRequest.process_instance_id == process_instance_id,
                    ActionRequest.id == action_request_id,
                )
                .with_for_update(of=(ActionRequest, ProcessInstance))
            )
        ).one_or_none()
        if row is None:
            raise ActionExecutionRejected("action revision not found")
        request, action_revision, policy, process = row[0], row[1], row[2], row[3]
        if process.status in {"paused", "completed", "cancelled", "failed"}:
            raise ActionExecutionRejected(
                f"process state does not permit action execution: {process.status}"
            )
        if request.current_revision != revision:
            raise ActionExecutionRejected("action revision is no longer current")
        outcome = PermissionOutcome(policy.outcome)
        retryable_statuses = {
            ActionRequestStatus.EXECUTING.value,
            ActionRequestStatus.UNKNOWN.value,
            ActionRequestStatus.RECONCILING.value,
            ActionRequestStatus.SUCCEEDED.value,
            ActionRequestStatus.FAILED.value,
            ActionRequestStatus.CONFLICT.value,
        }
        if outcome is PermissionOutcome.ALLOW:
            if request.status not in {ActionRequestStatus.ALLOWED.value, *retryable_statuses}:
                raise ActionExecutionRejected("action is not currently allowed to execute")
        elif outcome is PermissionOutcome.REQUIRE_APPROVAL:
            approval = await session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.tenant_id == tenant_id,
                    ApprovalRequest.process_instance_id == process_instance_id,
                    ApprovalRequest.action_request_id == action_request_id,
                    ApprovalRequest.revision == revision,
                    ApprovalRequest.payload_hash == action_revision.payload_hash,
                )
            )
            if approval is None or approval.status != ApprovalStatus.APPROVED.value:
                raise ActionExecutionRejected("exact action payload has not been approved")
            if approval.expires_at is not None and approval.expires_at <= datetime.now(UTC):
                raise ActionExecutionRejected("exact action approval has expired")
            decision = await session.scalar(
                select(ApprovalDecision).where(
                    ApprovalDecision.tenant_id == tenant_id,
                    ApprovalDecision.process_instance_id == process_instance_id,
                    ApprovalDecision.approval_request_id == approval.id,
                    ApprovalDecision.decision == "approved",
                    ApprovalDecision.payload_hash == action_revision.payload_hash,
                )
            )
            if decision is None:
                raise ActionExecutionRejected("immutable approval decision is missing")
            if request.status not in {ActionRequestStatus.APPROVED.value, *retryable_statuses}:
                raise ActionExecutionRejected("approved action is no longer executable")
        else:
            raise ActionExecutionRejected("policy denied action execution")
        return request, action_revision, policy, process

    async def _record_success(
        self,
        tenant_id: UUID,
        action_request_id: UUID,
        key: str,
        provider_result: ProviderActionResult,
    ) -> ActionExecutionResult:
        return await self._record_terminal(
            tenant_id,
            action_request_id,
            key,
            ActionAttemptStatus.SUCCEEDED,
            provider_reference=provider_result.provider_reference,
            result=provider_result.result,
            facts=provider_result.facts,
        )

    async def _record_failure(
        self, tenant_id: UUID, action_request_id: UUID, key: str, error: str
    ) -> ActionExecutionResult:
        return await self._record_terminal(
            tenant_id, action_request_id, key, ActionAttemptStatus.FAILED, error=error
        )

    async def _record_conflict(
        self,
        tenant_id: UUID,
        action_request_id: UUID,
        key: str,
        conflict: ActionConflict,
    ) -> ActionExecutionResult:
        return await self._record_terminal(
            tenant_id,
            action_request_id,
            key,
            ActionAttemptStatus.CONFLICT,
            facts=conflict.facts,
            error=conflict.message,
            conflict=conflict,
        )

    async def _record_unknown(
        self, tenant_id: UUID, action_request_id: UUID, key: str, error: str
    ) -> ActionExecutionResult:
        return await self._record_terminal(
            tenant_id, action_request_id, key, ActionAttemptStatus.UNKNOWN, error=error
        )

    async def _record_terminal(
        self,
        tenant_id: UUID,
        action_request_id: UUID,
        key: str,
        status: ActionAttemptStatus,
        *,
        provider_reference: str | None = None,
        result: dict[str, object] | None = None,
        facts: tuple[FactObservation, ...] = (),
        error: str | None = None,
        conflict: ActionConflict | None = None,
    ) -> ActionExecutionResult:
        async with self._session_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            attempt = await session.scalar(
                select(ActionAttempt).where(ActionAttempt.idempotency_key == key).with_for_update()
            )
            request = await session.scalar(
                select(ActionRequest).where(ActionRequest.id == action_request_id).with_for_update()
            )
            if attempt is None or request is None:
                raise RuntimeError("action execution state disappeared")
            if attempt.status in {
                ActionAttemptStatus.SUCCEEDED.value,
                ActionAttemptStatus.FAILED.value,
                ActionAttemptStatus.CONFLICT.value,
            }:
                return self._result(attempt)
            attempt.status = status.value
            attempt.provider_reference = provider_reference
            attempt.result = result
            attempt.conflict = conflict.model_dump(mode="json") if conflict is not None else None
            attempt.facts = [fact.model_dump(mode="json") for fact in facts]
            attempt.error = error[:2000] if error else None
            attempt.completed_at = datetime.now(UTC)
            request.status = status.value
            await session.flush()
            return self._result(attempt)

    @staticmethod
    def _result(attempt: ActionAttempt) -> ActionExecutionResult:
        return ActionExecutionResult(
            action_request_id=attempt.action_request_id,
            attempt_id=attempt.id,
            status=ActionAttemptStatus(attempt.status),
            idempotency_key=attempt.idempotency_key,
            provider_reference=attempt.provider_reference,
            result=attempt.result,
            facts=tuple(FactObservation.model_validate(fact) for fact in attempt.facts),
            error=attempt.error,
            conflict=(
                ActionConflict.model_validate(attempt.conflict)
                if attempt.conflict is not None
                else None
            ),
        )
