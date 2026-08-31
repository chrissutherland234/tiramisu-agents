"""Add durable process interventions, operator controls, late-event policy, and lineage.

Revision ID: 20260831_10
Revises: 20260831_09
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260831_10"
down_revision: str | None = "20260831_09"
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
        "process_instances",
        sa.Column(
            "late_event_policy", sa.String(length=32), server_default="record_only", nullable=False
        ),
    )
    op.create_check_constraint(
        "late_event_policy_valid",
        "process_instances",
        "late_event_policy IN ('record_only')",
    )
    op.add_column(
        "action_requests",
        sa.Column("supersedes_action_request_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_action_requests_superseded_action",
        "action_requests",
        "action_requests",
        ["tenant_id", "process_instance_id", "supersedes_action_request_id"],
        ["tenant_id", "process_instance_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "process_interventions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("error_type", sa.String(length=150), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("source_event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "source_review_command_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "source_action_attempt_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_timer_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("resolved_by_command_id", postgresql.UUID(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('turn_failure', 'action_chain_limit')", name="kind_valid"),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="status_valid"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_process_interventions_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_interventions"),
        sa.UniqueConstraint(
            "tenant_id", "process_instance_id", "agent_turn_id", name="uq_process_intervention_turn"
        ),
    )
    op.create_index(
        "ix_process_interventions_open",
        "process_interventions",
        ["tenant_id", "process_instance_id", "status"],
    )
    op.create_table(
        "process_control_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intervention_id", postgresql.UUID(as_uuid=True)),
        sa.Column("command_type", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "command_type IN ('retry', 'wake', 'takeover', 'resume')", name="command_type_valid"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_process_control_commands_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_process_control_commands"),
    )
    op.create_index(
        "ix_process_control_commands_process_created",
        "process_control_commands",
        ["tenant_id", "process_instance_id", "created_at"],
    )

    _enable_tenant_rls("process_interventions")
    _enable_tenant_rls("process_control_commands")
    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT, UPDATE ON process_interventions TO tiramisu_app;
                    GRANT SELECT, INSERT ON process_control_commands TO tiramisu_app;
                    GRANT UPDATE (status, current_wake_conditions, updated_at)
                        ON process_instances TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_process_control_commands_process_created", table_name="process_control_commands"
    )
    op.drop_table("process_control_commands")
    op.drop_index("ix_process_interventions_open", table_name="process_interventions")
    op.drop_table("process_interventions")
    op.drop_constraint(
        "fk_action_requests_superseded_action", "action_requests", type_="foreignkey"
    )
    op.drop_column("action_requests", "supersedes_action_request_id")
    op.drop_constraint("late_event_policy_valid", "process_instances", type_="check")
    op.drop_column("process_instances", "late_event_policy")
