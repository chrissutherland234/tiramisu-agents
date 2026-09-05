"""Add immutable quarantine resolution audit records.

Revision ID: 20260905_18
Revises: 20260905_17
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_18"
down_revision: str | None = "20260905_17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_resolution_commands",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_status", sa.String(32), nullable=False),
        sa.Column("previous_reason", sa.String(500)),
        sa.Column("bound_references", postgresql.JSONB(), nullable=False),
        sa.Column("delivery_scheduled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_event_resolution_commands"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["event_inbox.tenant_id", "event_inbox.id"],
            name="fk_event_resolution_commands_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_event_resolution_commands_process",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("tenant_id", "event_id", name="uq_event_resolution_commands_event"),
        sa.CheckConstraint(
            "previous_status IN ('pending', 'rejected')", name="previous_status_valid"
        ),
    )
    op.create_index(
        "ix_event_resolution_commands_created",
        "event_resolution_commands",
        ["tenant_id", "created_at", "id"],
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text("ALTER TABLE event_resolution_commands ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE event_resolution_commands FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON event_resolution_commands "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    op.execute(
        sa.text("""
        DO $grant_runtime$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                GRANT SELECT, INSERT ON event_resolution_commands TO tiramisu_app;
            END IF;
        END
        $grant_runtime$
    """)
    )


def downgrade() -> None:
    op.drop_index("ix_event_resolution_commands_created", table_name="event_resolution_commands")
    op.drop_table("event_resolution_commands")
