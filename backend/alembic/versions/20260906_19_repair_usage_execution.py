"""Repair databases that applied the initial usage ledger before execution identity.

Revision ID: 20260906_19
Revises: 20260905_18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260906_19"
down_revision: str | None = "20260905_18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("model_usage_ledger")}
    if "execution_id" not in columns:
        op.add_column(
            "model_usage_ledger",
            sa.Column(
                "execution_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
                server_default=sa.text("'00000000-0000-0000-0000-000000000000'::uuid"),
            ),
        )
    expected = {
        "tenant_id",
        "process_instance_id",
        "agent_turn_id",
        "execution_id",
        "attempt_number",
    }
    constraint = next(
        item
        for item in inspector.get_unique_constraints("model_usage_ledger")
        if item["name"] == "uq_model_usage_turn_attempt"
    )
    if set(constraint["column_names"]) != expected:
        op.drop_constraint("uq_model_usage_turn_attempt", "model_usage_ledger", type_="unique")
        op.create_unique_constraint(
            "uq_model_usage_turn_attempt",
            "model_usage_ledger",
            ["tenant_id", "process_instance_id", "agent_turn_id", "execution_id", "attempt_number"],
        )


def downgrade() -> None:
    # Revision 16's current schema already includes execution_id. Keep that
    # canonical schema and all recorded spend when crossing this repair revision.
    pass
