"""External correlations plus durable event inbox and delivery outbox."""

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ExternalCorrelation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "external_correlations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_external_correlations_process_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "resource_type",
            "external_id",
            name="uq_external_correlations_identity",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(500), nullable=False)


class EventInbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "event_inbox"
    __table_args__ = (
        CheckConstraint(
            "correlation_status IN ('pending', 'matched', 'rejected')",
            name="correlation_status_valid",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_event_inbox_process_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_event_inbox_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "source", "source_event_id", name="uq_event_inbox_source_event"
        ),
        Index("ix_event_inbox_pending", "tenant_id", "correlation_status", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(500), nullable=False)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False)
    event_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    correlation_status: Mapped[str] = mapped_column(
        String(32), server_default="pending", nullable=False
    )
    correlation_reason: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')", name="status_valid"
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_outbox_messages_process_instance",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "causation_event_id"],
            ["event_inbox.tenant_id", "event_inbox.id"],
            name="fk_outbox_messages_causation_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "deduplication_key", name="uq_outbox_messages_dedup"),
        Index("ix_outbox_messages_dispatch", "status", "available_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    causation_event_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    message_type: Mapped[str] = mapped_column(String(150), nullable=False)
    destination: Mapped[str] = mapped_column(String(255), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), server_default="pending", nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(2000))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
