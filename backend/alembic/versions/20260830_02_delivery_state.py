"""Add correlation diagnostics and recoverable outbox claims.

Revision ID: 20260830_02
Revises: 20260830_01
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_02"
down_revision: str | None = "20260830_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "external_correlations",
        "external_id",
        existing_type=sa.String(255),
        type_=sa.String(500),
        existing_nullable=False,
    )
    op.alter_column(
        "event_inbox",
        "source_event_id",
        existing_type=sa.String(255),
        type_=sa.String(500),
        existing_nullable=False,
    )
    op.add_column("event_inbox", sa.Column("correlation_reason", sa.String(500)))
    op.add_column("outbox_messages", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column("outbox_messages", sa.Column("claim_token", sa.Uuid()))
    op.create_check_constraint(
        op.f("ck_outbox_messages_claim_state_consistent"),
        "outbox_messages",
        "(status = 'publishing' AND claimed_at IS NOT NULL AND claim_token IS NOT NULL) "
        "OR (status <> 'publishing' AND claimed_at IS NULL AND claim_token IS NULL)",
    )
    op.drop_constraint(
        op.f("ck_process_instances_status_valid"),
        "process_instances",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_process_instances_status_valid"),
        "process_instances",
        "status IN ('active', 'waiting', 'review', 'paused', 'completed', 'cancelled', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_process_instances_status_valid"),
        "process_instances",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_process_instances_status_valid"),
        "process_instances",
        "status IN ('active', 'waiting', 'review', 'completed', 'cancelled', 'failed')",
    )
    op.execute(
        sa.text(
            "ALTER TABLE outbox_messages "
            "DROP CONSTRAINT IF EXISTS ck_outbox_messages_claim_state_consistent"
        )
    )
    op.execute(sa.text("ALTER TABLE outbox_messages DROP COLUMN IF EXISTS claim_token"))
    op.drop_column("outbox_messages", "claimed_at")
    op.drop_column("event_inbox", "correlation_reason")
    op.alter_column(
        "event_inbox",
        "source_event_id",
        existing_type=sa.String(500),
        type_=sa.String(255),
        existing_nullable=False,
    )
    op.alter_column(
        "external_correlations",
        "external_id",
        existing_type=sa.String(500),
        type_=sa.String(255),
        existing_nullable=False,
    )
