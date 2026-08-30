"""Idempotently persist action proposals before any side effect is possible."""

import json
from dataclasses import dataclass
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
from tiramisu_agents.db.models.actions import (
    ActionPolicyRecord,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
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
    ) -> tuple[PersistedAction, ...]:
        await set_tenant_context(session, tenant_id)
        return tuple(
            [
                await self._persist_action(
                    session,
                    tenant_id=tenant_id,
                    process_instance_id=process_instance_id,
                    agent_turn_id=agent_turn_id,
                    process_definition_version=process_definition_version,
                    based_on_event_ids=tuple(str(value) for value in decision.based_on_event_ids),
                    action=action,
                    policy=policy,
                )
                for action in decision.actions
            ]
        )

    async def _persist_action(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        process_instance_id: UUID,
        agent_turn_id: UUID,
        process_definition_version: str,
        based_on_event_ids: tuple[str, ...],
        action: ActionProposal,
        policy: ConfiguredActionPolicy,
    ) -> PersistedAction:
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
        )
