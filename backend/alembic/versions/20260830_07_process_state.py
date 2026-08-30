"""Add durable process knowledge, memory, and state revisions.

Revision ID: 20260830_07
Revises: 20260830_06
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_07"
down_revision: str | None = "20260830_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "action_attempts",
        sa.Column(
            "facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "authoritative_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "memory_summary_source_review_command_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "memory_summary_source_action_attempt_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "memory_summary_source_timer_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "customer_claims",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "fact_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("process_instances", sa.Column("memory_summary", sa.Text()))
    op.add_column(
        "process_instances",
        sa.Column(
            "memory_summary_source_event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column(
            "open_commitments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "process_instances",
        sa.Column("state_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_process_instances_state_version_nonnegative"),
        "process_instances",
        "state_version >= 0",
    )
    op.create_table(
        "process_state_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("decision_status", sa.String(32), nullable=False),
        sa.Column("process_status", sa.String(32), nullable=False),
        sa.Column("authoritative_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("customer_claims", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fact_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("memory_summary", sa.Text()),
        sa.Column(
            "memory_summary_source_event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "memory_summary_source_review_command_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "memory_summary_source_action_attempt_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "memory_summary_source_timer_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("open_commitments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("based_on_event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "based_on_review_command_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "based_on_action_attempt_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("based_on_timer_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint(
            "decision_status IN ('active', 'waiting', 'completed', 'escalated')",
            name="decision_status_valid",
        ),
        sa.CheckConstraint(
            "process_status IN "
            "('active', 'waiting', 'review', 'paused', 'completed', 'cancelled', 'failed')",
            name="process_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_process_state_revisions_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_state_revisions"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            name="uq_process_state_revision_turn",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "version",
            name="uq_process_state_revision_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "decision_id",
            name="uq_process_state_revision_decision",
        ),
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(sa.text("ALTER TABLE process_state_revisions ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE process_state_revisions FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON process_state_revisions "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT ON process_state_revisions TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_table("process_state_revisions")
    op.drop_constraint(
        op.f("ck_process_instances_state_version_nonnegative"),
        "process_instances",
        type_="check",
    )
    op.drop_column("process_instances", "state_version")
    op.drop_column("process_instances", "open_commitments")
    op.drop_column("process_instances", "memory_summary_source_event_ids")
    op.drop_column("process_instances", "memory_summary_source_timer_ids")
    op.drop_column("process_instances", "memory_summary_source_action_attempt_ids")
    op.drop_column("process_instances", "memory_summary_source_review_command_ids")
    op.drop_column("process_instances", "memory_summary")
    op.drop_column("process_instances", "fact_provenance")
    op.drop_column("process_instances", "customer_claims")
    op.drop_column("process_instances", "authoritative_facts")
    op.drop_column("action_attempts", "facts")
