"""Add the immutable model token/cost usage ledger.

Revision ID: 20260905_16
Revises: 20260905_15
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_16"
down_revision: str | None = "20260905_15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_usage_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("'00000000-0000-0000-0000-000000000000'::uuid"),
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("price_table_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        sa.CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        sa.CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        sa.CheckConstraint("cost_micros >= 0", name="cost_micros_nonnegative"),
        sa.CheckConstraint("price_table_version >= 1", name="price_table_version_positive"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_model_usage_ledger_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_usage_ledger"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            "execution_id",
            "attempt_number",
            name="uq_model_usage_turn_attempt",
        ),
    )
    op.create_index(
        "ix_model_usage_spend",
        "model_usage_ledger",
        ["tenant_id", "process_instance_id"],
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text("ALTER TABLE model_usage_ledger ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE model_usage_ledger FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON model_usage_ledger "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    # Append-only ledger: SELECT and INSERT, deliberately no UPDATE or DELETE.
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT ON model_usage_ledger TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_model_usage_spend", table_name="model_usage_ledger")
    op.drop_table("model_usage_ledger")
