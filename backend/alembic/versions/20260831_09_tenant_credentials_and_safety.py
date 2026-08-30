"""Add tenant-scoped bearer credentials and immutable safety events.

Revision ID: 20260831_09
Revises: 20260830_08
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831_09"
down_revision: str | None = "20260830_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_tenant_rls(table: str) -> None:
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
    op.execute(sa.text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
    op.execute(
        sa.text(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def upgrade() -> None:
    op.create_table(
        "tenant_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "roles",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL AND revoked_by_actor_id IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL "
            "AND revoked_by_actor_id IS NOT NULL)",
            name="revocation_state_consistent",
        ),
        sa.CheckConstraint("jsonb_typeof(scopes) = 'array'", name="scopes_array"),
        sa.CheckConstraint("jsonb_typeof(roles) = 'array'", name="roles_array"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_credentials_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_credentials"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_credentials_tenant_id"),
    )
    op.create_index(
        "ix_tenant_credentials_tenant_status",
        "tenant_credentials",
        ["tenant_id", "status"],
    )
    op.create_table(
        "tenant_safety_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=False),
        sa.Column("new_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "previous_status IN ('active', 'suspended')", name="previous_valid"
        ),
        sa.CheckConstraint("new_status IN ('active', 'suspended')", name="new_valid"),
        sa.CheckConstraint("previous_status <> new_status", name="status_changed"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_safety_events_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_safety_events"),
    )
    op.create_index(
        "ix_tenant_safety_events_tenant_created",
        "tenant_safety_events",
        ["tenant_id", "created_at"],
    )

    _enable_tenant_rls("tenant_credentials")
    _enable_tenant_rls("tenant_safety_events")
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT ON tenant_credentials TO tiramisu_app;
                    GRANT SELECT ON tenant_safety_events TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_safety_events_tenant_created", table_name="tenant_safety_events"
    )
    op.drop_table("tenant_safety_events")
    op.drop_index("ix_tenant_credentials_tenant_status", table_name="tenant_credentials")
    op.drop_table("tenant_credentials")
