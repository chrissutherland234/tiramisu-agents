"""Add explicit dead letters and attributed outbox requeue operations.

Revision ID: 20260831_11
Revises: 20260831_10
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831_11"
down_revision: str | None = "20260831_10"
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
    op.add_column(
        "outbox_messages",
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True)),
    )
    op.drop_constraint(op.f("ck_outbox_messages_status_valid"), "outbox_messages", type_="check")
    op.execute(
        sa.text(
            "UPDATE outbox_messages SET status = 'dead_letter', "
            "dead_lettered_at = COALESCE(updated_at, now()) WHERE status = 'failed'"
        )
    )
    op.create_check_constraint(
        "status_valid",
        "outbox_messages",
        "status IN ('pending', 'publishing', 'published', 'dead_letter')",
    )
    op.create_check_constraint(
        "dead_letter_state_consistent",
        "outbox_messages",
        "(status = 'dead_letter' AND dead_lettered_at IS NOT NULL) "
        "OR (status <> 'dead_letter' AND dead_lettered_at IS NULL)",
    )
    op.create_unique_constraint(
        "uq_outbox_messages_tenant_id_ref",
        "outbox_messages",
        ["tenant_id", "id"],
    )
    op.create_table(
        "outbox_recovery_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbox_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_attempt_count", sa.Integer(), nullable=False),
        sa.Column("previous_error", sa.String(length=2000)),
        sa.Column("previous_dead_lettered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("command_type IN ('requeue')", name="command_type_valid"),
        sa.CheckConstraint("previous_attempt_count > 0", name="previous_attempt_count_positive"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "outbox_message_id"],
            ["outbox_messages.tenant_id", "outbox_messages.id"],
            name="fk_outbox_recovery_commands_message",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_recovery_commands"),
    )
    op.create_index(
        "ix_outbox_recovery_commands_message_created",
        "outbox_recovery_commands",
        ["tenant_id", "outbox_message_id", "created_at"],
    )
    _enable_tenant_rls("outbox_recovery_commands")
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT ON outbox_recovery_commands TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_recovery_commands_message_created",
        table_name="outbox_recovery_commands",
    )
    op.drop_table("outbox_recovery_commands")
    op.drop_constraint(op.f("uq_outbox_messages_tenant_id_ref"), "outbox_messages", type_="unique")
    op.drop_constraint(
        op.f("ck_outbox_messages_dead_letter_state_consistent"),
        "outbox_messages",
        type_="check",
    )
    op.drop_constraint(op.f("ck_outbox_messages_status_valid"), "outbox_messages", type_="check")
    op.execute(sa.text("UPDATE outbox_messages SET status = 'failed' WHERE status = 'dead_letter'"))
    op.create_check_constraint(
        "status_valid",
        "outbox_messages",
        "status IN ('pending', 'publishing', 'published', 'failed')",
    )
    op.drop_column("outbox_messages", "dead_lettered_at")
