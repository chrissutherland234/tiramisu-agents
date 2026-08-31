"""Durable process instances and immutable state revisions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
        CheckConstraint(
            "late_event_policy IN ('record_only')",
            name="late_event_policy_valid",
        ),
        CheckConstraint(
            "client_pack_fingerprint ~ '^[0-9a-f]{64}$'",
            name="client_pack_fingerprint_sha256",
        ),
        CheckConstraint(
            "process_definition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="process_definition_fingerprint_sha256",
        ),
        CheckConstraint(
            "deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
            name="deployment_id_valid",
        ),
        CheckConstraint(
            "deployment_release_fingerprint ~ '^[0-9a-f]{64}$'",
            name="deployment_release_fingerprint_sha256",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_process_instances_tenant_id_id"),
        UniqueConstraint("tenant_id", "workflow_id", name="uq_process_instances_tenant_workflow"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    process_type: Mapped[str] = mapped_column(String(100), nullable=False)
    definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extension_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_pack_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    process_definition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(63), nullable=False)
    deployment_release_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    temporal_task_queue: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    current_run_id: Mapped[str | None] = mapped_column(String(255))
    late_event_policy: Mapped[str] = mapped_column(
        String(32), server_default="record_only", nullable=False
    )
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
    current_wake_conditions: Mapped[list[dict[str, Any]]] = mapped_column(
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
    wake_conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    based_on_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_review_command_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_action_attempt_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    based_on_timer_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class ProcessIntervention(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable operator-visible record of an orchestration failure or safety stop."""

    __tablename__ = "process_interventions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('turn_failure', 'action_chain_limit')",
            name="kind_valid",
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="status_valid"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_process_interventions_process_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            name="uq_process_intervention_turn",
        ),
        Index(
            "ix_process_interventions_open",
            "tenant_id",
            "process_instance_id",
            "status",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="open", nullable=False)
    error_type: Mapped[str] = mapped_column(String(150), nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_review_command_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_action_attempt_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_timer_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    resolved_by_command_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessControlCommand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable, attributed operator command for one process mailbox."""

    __tablename__ = "process_control_commands"
    __table_args__ = (
        CheckConstraint(
            "command_type IN ('retry', 'wake', 'takeover', 'resume')",
            name="command_type_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_process_control_commands_process_instance",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_process_control_commands_process_created",
            "tenant_id",
            "process_instance_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    intervention_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    command_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
