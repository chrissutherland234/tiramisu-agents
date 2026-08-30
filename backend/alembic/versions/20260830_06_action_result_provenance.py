"""Add action-result provenance to action revisions.

Revision ID: 20260830_06
Revises: 20260830_05
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_06"
down_revision: str | None = "20260830_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "action_revisions",
        sa.Column(
            "based_on_action_attempt_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_action_attempt_ref",
        "action_attempts",
        ["tenant_id", "process_instance_id", "action_request_id", "id"],
    )
    op.create_table(
        "action_reconciliation_decisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=False),
        sa.Column("evidence", sa.String(10_000), nullable=False),
        sa.Column("provider_reference", sa.String(500)),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("previous_status IN ('unknown', 'reconciling')", name="previous_valid"),
        sa.CheckConstraint("resolution IN ('succeeded', 'failed')", name="resolution_valid"),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name="pk_action_reconciliation_decisions"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_attempt_id",
            name="uq_action_reconciliation_decision_attempt",
        ),
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text("ALTER TABLE action_reconciliation_decisions ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE action_reconciliation_decisions FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON action_reconciliation_decisions "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT ON action_reconciliation_decisions TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_table("action_reconciliation_decisions")
    op.drop_constraint("uq_action_attempt_ref", "action_attempts", type_="unique")
    op.drop_column("action_revisions", "based_on_action_attempt_ids")
