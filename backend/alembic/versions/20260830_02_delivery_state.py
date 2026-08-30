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


def downgrade() -> None:
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
