"""Add tenant-scoped circuit breakers with audited transitions.

Revision ID: 20260905_17
Revises: 20260905_16
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_17"
down_revision: str | None = "20260905_16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "circuit_breakers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("target", sa.String(200), server_default="", nullable=False),
        sa.Column("tripped", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "scope IN ('model_calls', 'outbound_messages', 'capability', 'all')",
            name="scope_valid",
        ),
        sa.CheckConstraint(
            "(scope = 'capability' AND target <> '') OR (scope <> 'capability' AND target = '')",
            name="scope_target_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_circuit_breakers_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_circuit_breakers"),
    )
    op.create_index(
        "ix_circuit_breakers_latest",
        "circuit_breakers",
        ["tenant_id", "scope", "target", "created_at"],
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text("ALTER TABLE circuit_breakers ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE circuit_breakers FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON circuit_breakers "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    # Append-only transitions: SELECT and INSERT, deliberately no UPDATE or DELETE.
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT ON circuit_breakers TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_circuit_breakers_latest", table_name="circuit_breakers")
    op.drop_table("circuit_breakers")
