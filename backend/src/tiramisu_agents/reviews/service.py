"""Race-safe, idempotent human review commands."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.core.contracts.actions import ActionRequestStatus, ApprovalStatus
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.db.models.actions import ActionRequest, ApprovalRequest
from tiramisu_agents.db.models.events import OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.session import set_tenant_context


class ReviewConflict(ValueError):
    """Raised when a review command is stale or targets different durable state."""


@dataclass(frozen=True, slots=True)
class ReviewResult:
    command_id: str
    thread_status: str
    approval_status: str
    action_status: str


class ReviewService:
    async def apply(self, session: AsyncSession, command: ReviewCommand) -> ReviewResult:
        await set_tenant_context(session, command.tenant_id)
        existing = await session.scalar(
            select(ReviewMessage).where(
                ReviewMessage.tenant_id == command.tenant_id,
                ReviewMessage.id == command.command_id,
            )
        )

        thread = await session.scalar(
            select(ReviewThread).where(
                ReviewThread.tenant_id == command.tenant_id,
                ReviewThread.process_instance_id == command.process_instance_id,
                ReviewThread.id == command.review_thread_id,
            )
        )
        if thread is None:
            raise ReviewConflict("review thread not found")
        approval = await session.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.tenant_id == command.tenant_id,
                ApprovalRequest.process_instance_id == command.process_instance_id,
                ApprovalRequest.id == thread.approval_request_id,
            )
            .with_for_update()
        )
        request = await session.scalar(
            select(ActionRequest).where(
                ActionRequest.tenant_id == command.tenant_id,
                ActionRequest.process_instance_id == command.process_instance_id,
                ActionRequest.id == command.action_request_id,
            )
        )
        if approval is None or request is None:
            raise ReviewConflict("review target not found")
        if (
            approval.action_request_id != command.action_request_id
            or approval.revision != command.proposal_revision
            or request.current_revision != command.proposal_revision
        ):
            raise ReviewConflict("review command targets a stale proposal revision")
        if existing is not None:
            if (
                existing.review_thread_id != command.review_thread_id
                or existing.message_type != command.command_type.value
                or existing.actor_id != command.actor_id
                or existing.content != command.message
                or existing.proposal_revision != command.proposal_revision
                or (
                    command.command_type is ReviewCommandType.APPROVE
                    and command.expected_payload_hash != approval.payload_hash
                )
            ):
                raise ReviewConflict("review command ID was reused with different content")
            return self._result(command, thread, approval, request)
        if approval.status != ApprovalStatus.PENDING.value or thread.status != "open":
            raise ReviewConflict("review is no longer pending")

        if command.command_type is ReviewCommandType.APPROVE:
            if command.expected_payload_hash != approval.payload_hash:
                raise ReviewConflict("approval payload hash does not match the current proposal")
            approval.status = ApprovalStatus.APPROVED.value
            request.status = ActionRequestStatus.APPROVED.value
            thread.status = "approved"
            session.add(
                ApprovalDecision(
                    id=command.command_id,
                    tenant_id=command.tenant_id,
                    process_instance_id=command.process_instance_id,
                    approval_request_id=approval.id,
                    actor_id=command.actor_id,
                    decision="approved",
                    payload_hash=approval.payload_hash,
                    reason=command.message,
                )
            )
        elif command.command_type is ReviewCommandType.REJECT:
            approval.status = ApprovalStatus.REJECTED.value
            request.status = ActionRequestStatus.REJECTED.value
            thread.status = "rejected"
            session.add(
                ApprovalDecision(
                    id=command.command_id,
                    tenant_id=command.tenant_id,
                    process_instance_id=command.process_instance_id,
                    approval_request_id=approval.id,
                    actor_id=command.actor_id,
                    decision="rejected",
                    payload_hash=approval.payload_hash,
                    reason=command.message,
                )
            )
        elif command.command_type is ReviewCommandType.REQUEST_REVISION:
            approval.status = ApprovalStatus.SUPERSEDED.value
            request.status = ActionRequestStatus.SUPERSEDED.value
            thread.status = "revision_requested"
        elif command.command_type is not ReviewCommandType.COMMENT:
            raise ReviewConflict(f"unsupported review command: {command.command_type.value}")

        session.add(
            ReviewMessage(
                id=command.command_id,
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                review_thread_id=command.review_thread_id,
                actor_id=command.actor_id,
                message_type=command.command_type.value,
                content=command.message,
                proposal_revision=command.proposal_revision,
            )
        )
        workflow_id = await session.scalar(
            select(ProcessInstance.workflow_id).where(
                ProcessInstance.tenant_id == command.tenant_id,
                ProcessInstance.id == command.process_instance_id,
            )
        )
        if workflow_id is None:
            raise ReviewConflict("review process has no workflow identity")
        await session.execute(
            insert(OutboxMessage)
            .values(
                id=uuid4(),
                tenant_id=command.tenant_id,
                process_instance_id=command.process_instance_id,
                message_type="temporal.process_review",
                destination=workflow_id,
                deduplication_key=f"process-review:{command.command_id}",
                payload={
                    "command_id": str(command.command_id),
                    "command_type": command.command_type.value,
                    "review_thread_id": str(command.review_thread_id),
                    "action_request_id": str(command.action_request_id),
                    "proposal_revision": command.proposal_revision,
                },
            )
            .on_conflict_do_nothing(constraint="uq_outbox_messages_dedup")
        )
        await session.flush()
        return self._result(command, thread, approval, request)

    @staticmethod
    def _result(
        command: ReviewCommand,
        thread: ReviewThread,
        approval: ApprovalRequest,
        request: ActionRequest,
    ) -> ReviewResult:
        return ReviewResult(
            command_id=str(command.command_id),
            thread_status=thread.status,
            approval_status=approval.status,
            action_status=request.status,
        )
