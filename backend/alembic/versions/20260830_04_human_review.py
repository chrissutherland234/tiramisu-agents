"""Add conversational human-review records.

Revision ID: 20260830_04
Revises: 20260830_03
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_04"
down_revision: str | None = "20260830_03"
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


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    op.add_column(
        "action_revisions",
        sa.Column(
            "based_on_review_command_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "action_revisions",
        sa.Column(
            "based_on_timer_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_approval_request_ref",
        "approval_requests",
        ["tenant_id", "process_instance_id", "id"],
    )
    op.create_table(
        "review_threads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), server_default="open", nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('open', 'approved', 'rejected', 'revision_requested', 'cancelled')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "approval_request_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.process_instance_id",
                "approval_requests.id",
            ],
            name="fk_review_threads_approval_request",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_threads"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "approval_request_id",
            name="uq_review_thread_approval",
        ),
        sa.UniqueConstraint("tenant_id", "process_instance_id", "id", name="uq_review_thread_ref"),
    )
    op.create_table(
        "review_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content", sa.String(10_000)),
        sa.Column("proposal_revision", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "message_type IN ('approve', 'reject', 'request_revision', 'comment')",
            name="message_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "review_thread_id"],
            ["review_threads.tenant_id", "review_threads.process_instance_id", "review_threads.id"],
            name="fk_review_messages_thread",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_messages"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_review_message_command"),
    )
    op.create_table(
        "approval_decisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(10_000)),
        *_timestamps(),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="decision_valid"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "approval_request_id"],
            [
                "approval_requests.tenant_id",
                "approval_requests.process_instance_id",
                "approval_requests.id",
            ],
            name="fk_approval_decisions_request",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_decisions"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_approval_decision_command"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "approval_request_id",
            name="uq_approval_decision_request",
        ),
    )
    for table in ("review_threads", "review_messages", "approval_decisions"):
        _enable_tenant_rls(table)
    op.execute(
        sa.text("""
        DO $grant_runtime$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                GRANT SELECT, INSERT
                    ON review_threads, review_messages, approval_decisions
                    TO tiramisu_app;
                GRANT UPDATE ON review_threads TO tiramisu_app;
            END IF;
        END
        $grant_runtime$
    """)
    )


def downgrade() -> None:
    op.drop_table("approval_decisions")
    op.drop_table("review_messages")
    op.drop_table("review_threads")
    op.drop_constraint("uq_approval_request_ref", "approval_requests", type_="unique")
    op.execute(
        sa.text("ALTER TABLE action_revisions DROP COLUMN IF EXISTS based_on_review_command_ids")
    )
    op.execute(sa.text("ALTER TABLE action_revisions DROP COLUMN IF EXISTS based_on_timer_ids"))
