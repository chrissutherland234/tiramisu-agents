"""Add definitive provider conflict outcomes to action execution.

Revision ID: 20260902_14
Revises: 20260901_13
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_14"
down_revision: str | None = "20260901_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_status_constraint(table: str, expression: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_status_valid"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_status_valid"), table, expression)


def upgrade() -> None:
    _replace_status_constraint(
        "action_requests",
        "status IN ('allowed', 'denied', 'pending_approval', 'approved', "
        "'rejected', 'superseded', 'executing', 'succeeded', 'failed', "
        "'conflict', 'unknown', 'reconciling')",
    )
    _replace_status_constraint(
        "action_attempts",
        "status IN ('executing', 'succeeded', 'failed', 'conflict', 'unknown', 'reconciling')",
    )
    op.add_column(
        "action_attempts",
        sa.Column("conflict", postgresql.JSONB(astext_type=sa.Text())),
    )


def downgrade() -> None:
    # The previous schema cannot represent a conflict. Preserve the terminal
    # nature and human-readable error while translating populated rows before
    # restoring its narrower constraints.
    op.execute("UPDATE action_attempts SET status = 'failed' WHERE status = 'conflict'")
    op.execute("UPDATE action_requests SET status = 'failed' WHERE status = 'conflict'")
    op.drop_column("action_attempts", "conflict")
    _replace_status_constraint(
        "action_attempts",
        "status IN ('executing', 'succeeded', 'failed', 'unknown', 'reconciling')",
    )
    _replace_status_constraint(
        "action_requests",
        "status IN ('allowed', 'denied', 'pending_approval', 'approved', "
        "'rejected', 'superseded', 'executing', 'succeeded', 'failed', "
        "'unknown', 'reconciling')",
    )
