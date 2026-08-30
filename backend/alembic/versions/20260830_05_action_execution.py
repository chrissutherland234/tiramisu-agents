"""Add durable action execution attempts.

Revision ID: 20260830_05
Revises: 20260830_04
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_05"
down_revision: str | None = "20260830_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_action_requests_status_valid"), "action_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_action_requests_status_valid"),
        "action_requests",
        "status IN ('allowed', 'denied', 'pending_approval', 'approved', "
        "'rejected', 'superseded', 'executing', 'succeeded', 'failed', "
        "'unknown', 'reconciling')",
    )
    op.create_table(
        "action_attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("adapter_id", sa.String(150), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("provider_reference", sa.String(500)),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("error", sa.String(2000)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        sa.CheckConstraint(
            "status IN ('executing', 'succeeded', 'failed', 'unknown', 'reconciling')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name="pk_action_attempts"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_action_attempt_idempotency"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            "attempt_number",
            name="uq_action_attempt_number",
        ),
    )
    op.create_index(
        "ix_action_attempts_reconciliation",
        "action_attempts",
        ["tenant_id", "status", "updated_at"],
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text("ALTER TABLE action_attempts ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE action_attempts FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON action_attempts "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT, UPDATE ON action_attempts TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_action_attempts_reconciliation", table_name="action_attempts")
    op.drop_table("action_attempts")
    op.drop_constraint(op.f("ck_action_requests_status_valid"), "action_requests", type_="check")
    op.create_check_constraint(
        op.f("ck_action_requests_status_valid"),
        "action_requests",
        "status IN ('allowed', 'denied', 'pending_approval', 'approved', 'rejected', 'superseded')",
    )
