"""Durable action proposals, policy decisions, and approval requests."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ActionRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "action_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('allowed', 'denied', 'pending_approval', 'approved', "
            "'rejected', 'superseded', 'executing', 'succeeded', 'failed', "
            "'unknown', 'reconciling')",
            name="status_valid",
        ),
        CheckConstraint("current_revision >= 1", name="current_revision_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_action_requests_process_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "process_instance_id", "id", name="uq_action_request_ref"),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            "logical_action_key",
            name="uq_action_request_turn_logical_key",
        ),
        Index("ix_action_requests_process_status", "tenant_id", "process_instance_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    logical_action_key: Mapped[str] = mapped_column(String(200), nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    process_definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class ActionRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "action_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "action_request_id"],
            [
                "action_requests.tenant_id",
                "action_requests.process_instance_id",
                "action_requests.id",
            ],
            name="fk_action_revisions_action_request",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            name="uq_action_revision_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            "payload_hash",
            name="uq_action_revision_payload",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(String(1000), nullable=False)
    based_on_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_review_command_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    based_on_action_attempt_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    based_on_timer_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )


class ActionPolicyRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "action_policy_decisions"
    __table_args__ = (
        CheckConstraint("outcome IN ('allow', 'deny', 'require_approval')", name="outcome_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "action_request_id", "revision"],
            [
                "action_revisions.tenant_id",
                "action_revisions.process_instance_id",
                "action_revisions.action_request_id",
                "action_revisions.revision",
            ],
            name="fk_action_policy_decisions_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            name="uq_action_policy_decision_revision",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)


class ApprovalRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled', 'superseded')",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "process_instance_id",
                "action_request_id",
                "revision",
                "payload_hash",
            ],
            [
                "action_revisions.tenant_id",
                "action_revisions.process_instance_id",
                "action_revisions.action_request_id",
                "action_revisions.revision",
                "action_revisions.payload_hash",
            ],
            name="fk_approval_requests_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            name="uq_approval_request_revision",
        ),
        UniqueConstraint("tenant_id", "process_instance_id", "id", name="uq_approval_request_ref"),
        Index("ix_approval_requests_pending", "tenant_id", "status", "expires_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="pending", nullable=False)
    required_role: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "action_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(
            "status IN ('executing', 'succeeded', 'failed', 'unknown', 'reconciling')",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "action_request_id", "revision"],
            [
                "action_revisions.tenant_id",
                "action_revisions.process_instance_id",
                "action_revisions.action_request_id",
                "action_revisions.revision",
            ],
            name="fk_action_attempts_revision",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_action_attempt_idempotency"),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "id",
            name="uq_action_attempt_ref",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            "attempt_number",
            name="uq_action_attempt_number",
        ),
        Index("ix_action_attempts_reconciliation", "tenant_id", "status", "updated_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_id: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(500))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(2000))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionReconciliationDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "action_reconciliation_decisions"
    __table_args__ = (
        CheckConstraint("previous_status IN ('unknown', 'reconciling')", name="previous_valid"),
        CheckConstraint("resolution IN ('succeeded', 'failed')", name="resolution_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "action_request_id", "action_attempt_id"],
            [
                "action_attempts.tenant_id",
                "action_attempts.process_instance_id",
                "action_attempts.action_request_id",
                "action_attempts.id",
            ],
            name="fk_action_reconciliation_decisions_attempt",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_attempt_id",
            name="uq_action_reconciliation_decision_attempt",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action_request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action_attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    resolution: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[str] = mapped_column(String(10_000), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(500))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
