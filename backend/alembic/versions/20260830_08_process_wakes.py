"""Persist current and revision wake conditions.

Revision ID: 20260830_08
Revises: 20260830_07
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_08"
down_revision: str | None = "20260830_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "process_instances",
        sa.Column(
            "current_wake_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_state_revisions",
        sa.Column(
            "wake_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("process_state_revisions", "wake_conditions")
    op.drop_column("process_instances", "current_wake_conditions")
