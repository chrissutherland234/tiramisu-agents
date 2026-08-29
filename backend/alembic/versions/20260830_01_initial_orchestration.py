"""Create the tenant-scoped orchestration substrate.

Revision ID: 20260830_01
Revises:
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "tenants",
    "process_instances",
    "external_correlations",
    "event_inbox",
    "outbox_messages",
)


def _enable_tenant_rls(table: str, *, tenant_column: str = "tenant_id") -> None:
    predicate = f"{tenant_column} = nullif(current_setting('app.tenant_id', true), '')::uuid"
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
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="status_valid"),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_table(
        "process_instances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_type", sa.String(length=100), nullable=False),
        sa.Column("definition_version", sa.String(length=64), nullable=False),
        sa.Column("extension_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("current_run_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'waiting', 'review', 'completed', 'cancelled', 'failed')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_process_instances_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_instances"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_process_instances_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "workflow_id", name="uq_process_instances_tenant_workflow"
        ),
    )
    op.create_table(
        "external_correlations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_external_correlations_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_external_correlations"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "resource_type",
            "external_id",
            name="uq_external_correlations_identity",
        ),
    )
    op.create_table(
        "event_inbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("source_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "correlation_status", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "correlation_status IN ('pending', 'matched', 'rejected')",
            name="correlation_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_event_inbox_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_inbox"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_event_inbox_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "source", "source_event_id", name="uq_event_inbox_source_event"
        ),
    )
    op.create_index(
        "ix_event_inbox_pending",
        "event_inbox",
        ["tenant_id", "correlation_status", "created_at"],
    )
    op.create_table(
        "outbox_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("causation_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_type", sa.String(length=150), nullable=False),
        sa.Column("destination", sa.String(length=255), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "causation_event_id"],
            ["event_inbox.tenant_id", "event_inbox.id"],
            name="fk_outbox_messages_causation_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_outbox_messages_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_messages"),
        sa.UniqueConstraint("tenant_id", "deduplication_key", name="uq_outbox_messages_dedup"),
    )
    op.create_index("ix_outbox_messages_dispatch", "outbox_messages", ["status", "available_at"])

    _enable_tenant_rls("tenants", tenant_column="id")
    for table in TENANT_TABLES[1:]:
        _enable_tenant_rls(table)

    # The Compose runtime role is intentionally not the migration owner/superuser.
    # Managed deployments create an equivalent role before applying migrations.
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT USAGE ON SCHEMA public TO tiramisu_app;
                    GRANT SELECT
                        ON ALL TABLES IN SCHEMA public TO tiramisu_app;
                    GRANT INSERT, UPDATE, DELETE
                        ON process_instances, external_correlations,
                           event_inbox, outbox_messages
                        TO tiramisu_app;
                    REVOKE INSERT, UPDATE, DELETE ON tenants FROM tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_dispatch", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_index("ix_event_inbox_pending", table_name="event_inbox")
    op.drop_table("event_inbox")
    op.drop_table("external_correlations")
    op.drop_table("process_instances")
    op.drop_table("tenants")
