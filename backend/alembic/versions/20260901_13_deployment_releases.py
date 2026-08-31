"""Add logical tenant assignments and process-pinned deployment releases.

Revision ID: 20260901_13
Revises: 20260901_12
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260901_13"
down_revision: str | None = "20260901_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNKNOWN_FINGERPRINT = "0" * 64


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
    op.add_column(
        "tenants",
        sa.Column(
            "deployment_id",
            sa.String(length=63),
            server_default="unassigned",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "deployment_id_valid",
        "tenants",
        "deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
    )

    op.add_column(
        "process_instances",
        sa.Column("deployment_id", sa.String(length=63), nullable=True),
    )
    op.add_column(
        "process_instances",
        sa.Column("deployment_release_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "process_instances",
        sa.Column("temporal_task_queue", sa.String(length=255), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE process_instances SET deployment_id = 'unassigned', "
            "deployment_release_fingerprint = :unknown, temporal_task_queue = 'unassigned'"
        ).bindparams(unknown=_UNKNOWN_FINGERPRINT)
    )
    op.alter_column("process_instances", "deployment_id", nullable=False)
    op.alter_column("process_instances", "deployment_release_fingerprint", nullable=False)
    op.alter_column("process_instances", "temporal_task_queue", nullable=False)
    op.create_check_constraint(
        "deployment_id_valid",
        "process_instances",
        "deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
    )
    op.create_check_constraint(
        "deployment_release_fingerprint_sha256",
        "process_instances",
        "deployment_release_fingerprint ~ '^[0-9a-f]{64}$'",
    )

    op.create_table(
        "tenant_deployment_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_deployment_id", sa.String(length=63), nullable=False),
        sa.Column("new_deployment_id", sa.String(length=63), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "previous_deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
            name="previous_deployment_id_valid",
        ),
        sa.CheckConstraint(
            "new_deployment_id ~ '^[a-z][a-z0-9-]{0,62}$'",
            name="new_deployment_id_valid",
        ),
        sa.CheckConstraint(
            "previous_deployment_id <> new_deployment_id",
            name="deployment_changed",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_deployment_events_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_deployment_events"),
    )
    op.create_index(
        "ix_tenant_deployment_events_tenant_created",
        "tenant_deployment_events",
        ["tenant_id", "created_at"],
    )
    _enable_tenant_rls("tenant_deployment_events")
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT ON tenant_deployment_events TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_deployment_events_tenant_created",
        table_name="tenant_deployment_events",
    )
    op.drop_table("tenant_deployment_events")
    op.drop_constraint(
        op.f("ck_process_instances_deployment_release_fingerprint_sha256"),
        "process_instances",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_process_instances_deployment_id_valid"),
        "process_instances",
        type_="check",
    )
    op.drop_column("process_instances", "temporal_task_queue")
    op.drop_column("process_instances", "deployment_release_fingerprint")
    op.drop_column("process_instances", "deployment_id")
    op.drop_constraint(
        op.f("ck_tenants_deployment_id_valid"),
        "tenants",
        type_="check",
    )
    op.drop_column("tenants", "deployment_id")
