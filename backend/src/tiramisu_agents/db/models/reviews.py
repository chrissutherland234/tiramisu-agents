"""Durable conversational review and human decision records."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ReviewThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_threads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'approved', 'rejected', 'revision_requested', 'cancelled')",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "approval_request_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.process_instance_id",
                "approval_requests.id",
            ],
            name="fk_review_threads_approval_request",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "approval_request_id",
            name="uq_review_thread_approval",
        ),
        UniqueConstraint("tenant_id", "process_instance_id", "id", name="uq_review_thread_ref"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    approval_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="open", nullable=False)


class ReviewMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_messages"
    __table_args__ = (
        CheckConstraint(
            "message_type IN ('approve', 'reject', 'request_revision', 'comment')",
            name="message_type_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "review_thread_id"],
            ["review_threads.tenant_id", "review_threads.process_instance_id", "review_threads.id"],
            name="fk_review_messages_thread",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_review_message_command"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    review_thread_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(String(10_000))
    proposal_revision: Mapped[int] = mapped_column(nullable=False)


class ApprovalDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected')", name="decision_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "approval_request_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.process_instance_id",
                "approval_requests.id",
            ],
            name="fk_approval_decisions_request",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_approval_decision_command"),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "approval_request_id",
            name="uq_approval_decision_request",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    approval_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(10_000))
