"""Idempotently persist action proposals before any side effect is possible."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.action_policy import ConfiguredActionPolicy
from tiramisu_agents.core.contracts.actions import (
    ActionRequestStatus,
    ApprovalStatus,
    PermissionOutcome,
)
from tiramisu_agents.core.contracts.decisions import ActionProposal, AgentDecision
from tiramisu_agents.core.limits import require_action_parameters
from tiramisu_agents.db.models.actions import (
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import EventInbox
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


@dataclass(frozen=True, slots=True)
class CommunicationPolicy:
    outbound_action_types: frozenset[str]
    reply_event_types: frozenset[str]
    max_follow_ups_without_reply: int
    minimum_follow_up_interval: timedelta


def action_payload_hash(action: ActionProposal) -> str:
    payload = {
        "action_type": action.action_type,
        "parameters": action.model_dump(mode="json")["parameters"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


class ActionGateway:
    """Persist policy-classified proposals; execution is a later gateway stage."""

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
        process_status = await session.scalar(
            select(ProcessInstance.status).where(
                ProcessInstance.tenant_id == tenant_id,
                ProcessInstance.id == process_instance_id,
            )
        )
        if process_status is None:
            raise ActionPersistenceConflict("process instance not found")
        if process_status in {"paused", "completed", "cancelled", "failed"}:
            raise ActionPersistenceConflict(
                f"process state does not permit new actions: {process_status}"
            )
        revision_targets = await self._revision_targets(
            session,
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            review_command_ids=decision.based_on_review_command_ids,
        )
        used_targets: set[UUID] = set()
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
                and communication_policy is not None
                and workflow_now is not None
                and action.action_type in communication_policy.outbound_action_types
            ):
                await self._enforce_communication_policy(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
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
    async def _enforce_communication_policy(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        policy: CommunicationPolicy,
        workflow_now: datetime,
    ) -> None:
        last_reply_at = await session.scalar(
            select(EventInbox.received_at)
            .where(
                EventInbox.tenant_id == tenant_id,
                EventInbox.process_instance_id == process_instance_id,
                EventInbox.event_type.in_(policy.reply_event_types),
            )
            .order_by(EventInbox.received_at.desc())
            .limit(1)
        )
        sent_statuses = (
            ActionRequestStatus.ALLOWED.value,
            ActionRequestStatus.PENDING_APPROVAL.value,
            ActionRequestStatus.APPROVED.value,
            ActionRequestStatus.EXECUTING.value,
            ActionRequestStatus.SUCCEEDED.value,
            ActionRequestStatus.UNKNOWN.value,
            ActionRequestStatus.RECONCILING.value,
        )
        query = select(ActionRequest).where(
            ActionRequest.tenant_id == tenant_id,
            ActionRequest.process_instance_id == process_instance_id,
            ActionRequest.action_type.in_(policy.outbound_action_types),
            ActionRequest.status.in_(sent_statuses),
        )
        if last_reply_at is not None:
            query = query.where(ActionRequest.created_at > last_reply_at)
        prior = (
            await session.scalars(query.order_by(ActionRequest.created_at.desc(), ActionRequest.id))
        ).all()
        if len(prior) >= policy.max_follow_ups_without_reply:
            raise ActionPersistenceConflict("maximum follow-ups without a reply has been reached")
        if prior and workflow_now < prior[0].created_at + policy.minimum_follow_up_interval:
            raise ActionPersistenceConflict("minimum follow-up interval has not elapsed")

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
        status = {
            PermissionOutcome.ALLOW: ActionRequestStatus.ALLOWED,
            PermissionOutcome.DENY: ActionRequestStatus.DENIED,
            PermissionOutcome.REQUIRE_APPROVAL: ActionRequestStatus.PENDING_APPROVAL,
        }[policy_decision.outcome]
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
