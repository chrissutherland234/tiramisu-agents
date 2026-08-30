"""Durable process instances and immutable state revisions."""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ProcessInstance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "process_instances"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('active', 'waiting', 'review', 'paused', 'completed', 'cancelled', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("state_version >= 0", name="state_version_nonnegative"),
        UniqueConstraint("tenant_id", "id", name="uq_process_instances_tenant_id_id"),
        UniqueConstraint("tenant_id", "workflow_id", name="uq_process_instances_tenant_workflow"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    process_type: Mapped[str] = mapped_column(String(100), nullable=False)
    definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extension_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    current_run_id: Mapped[str | None] = mapped_column(String(255))
    authoritative_facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    customer_claims: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    fact_provenance: Mapped[dict[str, dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    memory_summary: Mapped[str | None] = mapped_column(Text)
    memory_summary_source_event_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    memory_summary_source_review_command_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    memory_summary_source_action_attempt_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    memory_summary_source_timer_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    open_commitments: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    state_version: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)


class ProcessStateRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable resulting process projection for one successfully applied agent turn."""

    __tablename__ = "process_state_revisions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "decision_status IN ('active', 'waiting', 'completed', 'escalated')",
            name="decision_status_valid",
        ),
        CheckConstraint(
            "process_status IN "
            "('active', 'waiting', 'review', 'paused', 'completed', 'cancelled', 'failed')",
            name="process_status_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_process_state_revisions_process_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            name="uq_process_state_revision_turn",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "version",
            name="uq_process_state_revision_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "decision_id",
            name="uq_process_state_revision_decision",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_status: Mapped[str] = mapped_column(String(32), nullable=False)
    process_status: Mapped[str] = mapped_column(String(32), nullable=False)
    authoritative_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    customer_claims: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fact_provenance: Mapped[dict[str, dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    memory_summary: Mapped[str | None] = mapped_column(Text)
    memory_summary_source_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    memory_summary_source_review_command_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False
    )
    memory_summary_source_action_attempt_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False
    )
    memory_summary_source_timer_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    open_commitments: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_review_command_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_action_attempt_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_timer_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
