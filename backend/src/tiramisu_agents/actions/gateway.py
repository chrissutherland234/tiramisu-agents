"""Idempotently persist action proposals before any side effect is possible."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.communications import (
    CommunicationPolicy,
    CommunicationSafetyBlocked,
)
from tiramisu_agents.communications.safety import CommunicationSafetyService
from tiramisu_agents.core.action_identity import action_payload_identity
from tiramisu_agents.core.action_policy import (
    ConfiguredActionPolicy,
    initial_action_request_status,
)
from tiramisu_agents.core.contracts.actions import (
    ActionAttemptStatus,
    ActionRequestStatus,
    ApprovalStatus,
    PermissionOutcome,
)
from tiramisu_agents.core.contracts.decisions import ActionProposal, AgentDecision
from tiramisu_agents.core.limits import require_action_parameters
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ReviewMessage, ReviewThread
from tiramisu_agents.db.session import set_tenant_context


class ActionPersistenceConflict(ValueError):
    """Raised when an idempotency identity is reused for different content."""


@dataclass(frozen=True, slots=True)
class PersistedAction:
    action_request_id: UUID
    revision: int
    payload_hash: str
    outcome: PermissionOutcome
    status: ActionRequestStatus
    approval_request_id: UUID | None
    review_thread_id: UUID | None


def action_payload_hash(action: ActionProposal) -> str:
    return action_payload_identity(
        action.action_type,
        action.model_dump(mode="json")["parameters"],
    )


class ActionGateway:
    """Persist policy-classified proposals; execution is a later gateway stage."""

    def __init__(self, communication_safety: CommunicationSafetyService | None = None) -> None:
        self._communication_safety = communication_safety or CommunicationSafetyService()

    async def persist_decision(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        agent_turn_id: UUID,
        process_definition_version: str,
        decision: AgentDecision,
        policy: ConfiguredActionPolicy,
        communication_policy: CommunicationPolicy | None = None,
        workflow_now: datetime | None = None,
    ) -> tuple[PersistedAction, ...]:
        await set_tenant_context(session, tenant_id)
        process = await session.scalar(
            select(ProcessInstance)
            .where(
                ProcessInstance.tenant_id == tenant_id,
                ProcessInstance.id == process_instance_id,
            )
            .with_for_update()
        )
        if process is None:
            raise ActionPersistenceConflict("process instance not found")
        if process.status in {"paused", "completed", "cancelled", "failed"}:
            raise ActionPersistenceConflict(
                f"process state does not permit new actions: {process.status}"
            )
        if communication_policy is not None and workflow_now is not None:
            lifetime_block = self._communication_safety.process_lifetime_block(
                process=process,
                policy=communication_policy,
                now=workflow_now,
            )
            if lifetime_block is not None:
                raise ActionPersistenceConflict(lifetime_block.message)
        revision_targets = await self._revision_targets(
            session,
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            review_command_ids=decision.based_on_review_command_ids,
        )
        used_targets: set[UUID] = set()
        conflicted_payload_hashes = await self._conflicted_payload_hashes(
            session,
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            action_attempt_ids=decision.based_on_action_attempt_ids,
        )
        persisted: list[PersistedAction] = []
        for action in decision.actions:
            existing_action_id = await session.scalar(
                select(ActionRequest.id).where(
                    ActionRequest.tenant_id == tenant_id,
                    ActionRequest.process_instance_id == process_instance_id,
                    ActionRequest.agent_turn_id == agent_turn_id,
                    ActionRequest.logical_action_key == action.logical_action_key,
                )
            )
            if (
                existing_action_id is None
                and action_payload_hash(action) in conflicted_payload_hashes
            ):
                raise ActionPersistenceConflict(
                    "decision repeats an action payload that just returned a definitive conflict"
                )
            if (
                existing_action_id is None
                and communication_policy is not None
                and workflow_now is not None
                and action.action_type in communication_policy.outbound_action_types
            ):
                await self._enforce_communication_policy(
                    session,
                    tenant_id=tenant_id,
                    process=process,
                    policy=communication_policy,
                    workflow_now=workflow_now,
                )
            matching_targets = tuple(
                request_id
                for request_id, action_type in revision_targets
                if action_type == action.action_type and request_id not in used_targets
            )
            supersedes_action_request_id = matching_targets[0] if matching_targets else None
            if supersedes_action_request_id is not None:
                used_targets.add(supersedes_action_request_id)
            persisted.append(
                await self._persist_action(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                    agent_turn_id=agent_turn_id,
                    process_definition_version=process_definition_version,
                    based_on_event_ids=tuple(str(value) for value in decision.based_on_event_ids),
                    based_on_review_command_ids=tuple(
                        str(value) for value in decision.based_on_review_command_ids
                    ),
                    based_on_action_attempt_ids=tuple(
                        str(value) for value in decision.based_on_action_attempt_ids
                    ),
                    based_on_timer_ids=decision.based_on_timer_ids,
                    supersedes_action_request_id=supersedes_action_request_id,
                    action=action,
                    policy=policy,
                )
            )
        return tuple(persisted)

    @staticmethod
    async def _conflicted_payload_hashes(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        action_attempt_ids: tuple[UUID, ...],
    ) -> frozenset[str]:
        if not action_attempt_ids:
            return frozenset()
        rows = (
            await session.execute(
                select(ActionRequest.action_type, ActionRevision.parameters)
                .join(
                    ActionAttempt,
                    (ActionAttempt.action_request_id == ActionRequest.id)
                    & (ActionAttempt.tenant_id == ActionRequest.tenant_id)
                    & (ActionAttempt.process_instance_id == ActionRequest.process_instance_id),
                )
                .join(
                    ActionRevision,
                    (ActionRevision.action_request_id == ActionAttempt.action_request_id)
                    & (ActionRevision.revision == ActionAttempt.revision)
                    & (ActionRevision.tenant_id == ActionAttempt.tenant_id)
                    & (ActionRevision.process_instance_id == ActionAttempt.process_instance_id),
                )
                .where(
                    ActionAttempt.tenant_id == tenant_id,
                    ActionAttempt.process_instance_id == process_instance_id,
                    ActionAttempt.id.in_(action_attempt_ids),
                    ActionAttempt.status == ActionAttemptStatus.CONFLICT.value,
                )
            )
        ).all()
        return frozenset(action_payload_identity(row.action_type, row.parameters) for row in rows)

    async def _enforce_communication_policy(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process: ProcessInstance,
        policy: CommunicationPolicy,
        workflow_now: datetime,
    ) -> None:
        try:
            snapshot = await self._communication_safety.inspect(
                session,
                tenant_id=tenant_id,
                process=process,
                policy=policy,
                now=workflow_now,
            )
            snapshot.require_allowed()
        except CommunicationSafetyBlocked as error:
            raise ActionPersistenceConflict(str(error)) from error

    @staticmethod
    async def _revision_targets(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        review_command_ids: tuple[UUID, ...],
    ) -> tuple[tuple[UUID, str], ...]:
        if not review_command_ids:
            return ()
        rows = (
            await session.execute(
                select(ActionRequest.id, ActionRequest.action_type)
                .join(ApprovalRequest, ApprovalRequest.action_request_id == ActionRequest.id)
                .join(ReviewThread, ReviewThread.approval_request_id == ApprovalRequest.id)
                .join(ReviewMessage, ReviewMessage.review_thread_id == ReviewThread.id)
                .where(
                    ReviewMessage.tenant_id == tenant_id,
                    ReviewMessage.process_instance_id == process_instance_id,
                    ReviewMessage.id.in_(review_command_ids),
                    ReviewMessage.message_type == "request_revision",
                )
            )
        ).all()
        return tuple((row.id, row.action_type) for row in rows)

    async def _persist_action(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        agent_turn_id: UUID,
        process_definition_version: str,
        based_on_event_ids: tuple[str, ...],
        based_on_review_command_ids: tuple[str, ...],
        based_on_action_attempt_ids: tuple[str, ...],
        based_on_timer_ids: tuple[str, ...],
        supersedes_action_request_id: UUID | None,
        action: ActionProposal,
        policy: ConfiguredActionPolicy,
    ) -> PersistedAction:
        try:
            require_action_parameters(action.parameters)
        except ValueError as error:
            raise ActionPersistenceConflict(str(error)) from error
        policy_decision = policy.evaluate(action)
        status = initial_action_request_status(policy_decision.outcome)
        inserted_id = await session.scalar(
            insert(ActionRequest)
            .values(
                id=action.action_request_id,
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                agent_turn_id=agent_turn_id,
                logical_action_key=action.logical_action_key,
                action_type=action.action_type,
                process_definition_version=process_definition_version,
                current_revision=1,
                status=status.value,
                supersedes_action_request_id=supersedes_action_request_id,
            )
            .on_conflict_do_nothing(constraint="uq_action_request_turn_logical_key")
            .returning(ActionRequest.id)
        )
        request = await session.scalar(
            select(ActionRequest).where(
                ActionRequest.tenant_id == tenant_id,
                ActionRequest.process_instance_id == process_instance_id,
                ActionRequest.agent_turn_id == agent_turn_id,
                ActionRequest.logical_action_key == action.logical_action_key,
            )
        )
        if request is None:
            raise ActionPersistenceConflict("action request identity could not be reserved")
        if request.action_type != action.action_type:
            raise ActionPersistenceConflict("logical action key was reused for another action type")
        if request.supersedes_action_request_id != supersedes_action_request_id:
            raise ActionPersistenceConflict("action revision lineage changed during replay")

        payload_hash = action_payload_hash(action)
        await session.execute(
            insert(ActionRevision)
            .values(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                action_request_id=request.id,
                revision=1,
                parameters=action.parameters,
                payload_hash=payload_hash,
                rationale=action.rationale,
                based_on_event_ids=list(based_on_event_ids),
                based_on_review_command_ids=list(based_on_review_command_ids),
                based_on_action_attempt_ids=list(based_on_action_attempt_ids),
                based_on_timer_ids=list(based_on_timer_ids),
            )
            .on_conflict_do_nothing(constraint="uq_action_revision_number")
        )
        revision = await session.scalar(
            select(ActionRevision).where(
                ActionRevision.tenant_id == tenant_id,
                ActionRevision.process_instance_id == process_instance_id,
                ActionRevision.action_request_id == request.id,
                ActionRevision.revision == 1,
            )
        )
        if revision is None or revision.payload_hash != payload_hash:
            raise ActionPersistenceConflict(
                "action revision identity was reused for another payload"
            )

        await session.execute(
            insert(ActionPolicyRecord)
            .values(
                tenant_id=tenant_id,
                process_instance_id=process_instance_id,
                action_request_id=request.id,
                revision=1,
                outcome=policy_decision.outcome.value,
                policy_version=policy_decision.policy_version,
                reason=policy_decision.reason,
            )
            .on_conflict_do_nothing(constraint="uq_action_policy_decision_revision")
        )
        stored_policy = await session.scalar(
            select(ActionPolicyRecord).where(
                ActionPolicyRecord.tenant_id == tenant_id,
                ActionPolicyRecord.process_instance_id == process_instance_id,
                ActionPolicyRecord.action_request_id == request.id,
                ActionPolicyRecord.revision == 1,
            )
        )
        if stored_policy is None:
            raise ActionPersistenceConflict("action policy decision was not persisted")
        stored_outcome = PermissionOutcome(stored_policy.outcome)

        approval_id: UUID | None = None
        review_thread_id: UUID | None = None
        if stored_outcome is PermissionOutcome.REQUIRE_APPROVAL:
            await session.execute(
                insert(ApprovalRequest)
                .values(
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                    action_request_id=request.id,
                    revision=1,
                    payload_hash=payload_hash,
                    status=ApprovalStatus.PENDING.value,
                )
                .on_conflict_do_nothing(constraint="uq_approval_request_revision")
            )
            approval = await session.scalar(
                select(ApprovalRequest).where(
                    ApprovalRequest.tenant_id == tenant_id,
                    ApprovalRequest.process_instance_id == process_instance_id,
                    ApprovalRequest.action_request_id == request.id,
                    ApprovalRequest.revision == 1,
                )
            )
            if approval is None or approval.payload_hash != payload_hash:
                raise ActionPersistenceConflict("approval is not bound to the current payload")
            approval_id = approval.id
            await session.execute(
                insert(ReviewThread)
                .values(
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                    approval_request_id=approval.id,
                    status="open",
                )
                .on_conflict_do_nothing(constraint="uq_review_thread_approval")
            )
            review_thread = await session.scalar(
                select(ReviewThread).where(
                    ReviewThread.tenant_id == tenant_id,
                    ReviewThread.process_instance_id == process_instance_id,
                    ReviewThread.approval_request_id == approval.id,
                )
            )
            if review_thread is None:
                raise ActionPersistenceConflict("approval review thread was not persisted")
            review_thread_id = review_thread.id

        persisted_status = ActionRequestStatus(request.status)
        if inserted_id is not None and persisted_status is not status:
            raise ActionPersistenceConflict("new action request has an unexpected status")
        return PersistedAction(
            action_request_id=request.id,
            revision=1,
            payload_hash=payload_hash,
            outcome=stored_outcome,
            status=persisted_status,
            approval_request_id=approval_id,
            review_thread_id=review_thread_id,
        )
