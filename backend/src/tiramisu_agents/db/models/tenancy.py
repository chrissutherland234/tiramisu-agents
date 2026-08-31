"""Tenant ownership, credentials, and live-safety audit boundaries."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        CheckConstraint(
            "deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
            name="deployment_id_valid",
        ),
    )

    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    deployment_id: Mapped[str] = mapped_column(
        String(63), server_default="unassigned", nullable=False
    )


class TenantDeploymentEvent(UUIDPrimaryKeyMixin, Base):
    """Immutable audit record for a tenant's logical deployment assignment."""

    __tablename__ = "tenant_deployment_events"
    __table_args__ = (
        CheckConstraint(
            "previous_deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
            name="previous_deployment_id_valid",
        ),
        CheckConstraint(
            "new_deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
            name="new_deployment_id_valid",
        ),
        CheckConstraint(
            "previous_deployment_id <> new_deployment_id",
            name="deployment_changed",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_deployment_events_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_tenant_deployment_events_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    previous_deployment_id: Mapped[str] = mapped_column(String(63), nullable=False)
    new_deployment_id: Mapped[str] = mapped_column(String(63), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A deployment-issued bearer credential; plaintext secrets are never stored."""

    __tablename__ = "tenant_credentials"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked')", name="status_valid"),
        CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL AND revoked_by_actor_id IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_actor_id IS NOT NULL)",
            name="revocation_state_consistent",
        ),
        CheckConstraint("jsonb_typeof(scopes) = 'array'", name="scopes_array"),
        CheckConstraint("jsonb_typeof(roles) = 'array'", name="roles_array"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_credentials_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tenant_credentials_tenant_id"),
        Index("ix_tenant_credentials_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    roles: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class TenantSafetyEvent(UUIDPrimaryKeyMixin, Base):
    """Immutable control-plane record for suspension and resumption."""

    __tablename__ = "tenant_safety_events"
    __table_args__ = (
        CheckConstraint("previous_status IN ('active', 'suspended')", name="previous_valid"),
        CheckConstraint("new_status IN ('active', 'suspended')", name="new_valid"),
        CheckConstraint("previous_status <> new_status", name="status_changed"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_safety_events_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_tenant_safety_events_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
