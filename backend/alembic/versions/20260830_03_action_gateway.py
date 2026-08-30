"""Add durable action proposal and permission-gateway records.

Revision ID: 20260830_03
Revises: 20260830_02
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260830_03"
down_revision: str | None = "20260830_02"
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
    op.create_table(
        "action_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("logical_action_key", sa.String(200), nullable=False),
        sa.Column("action_type", sa.String(100), nullable=False),
        sa.Column("process_definition_version", sa.String(64), nullable=False),
        sa.Column("current_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("current_revision >= 1", name="current_revision_positive"),
        sa.CheckConstraint(
            "status IN ('allowed', 'denied', 'pending_approval', 'approved', "
            "'rejected', 'superseded')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_action_requests_process_instance",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_action_requests"),
        sa.UniqueConstraint("tenant_id", "process_instance_id", "id", name="uq_action_request_ref"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            "logical_action_key",
            name="uq_action_request_turn_logical_key",
        ),
    )
    op.create_index(
        "ix_action_requests_process_status",
        "action_requests",
        ["tenant_id", "process_instance_id", "status"],
    )
    op.create_table(
        "action_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("rationale", sa.String(1000), nullable=False),
        sa.Column("based_on_event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "action_request_id"],
            [
                "action_requests.tenant_id",
                "action_requests.process_instance_id",
                "action_requests.id",
            ],
            name="fk_action_revisions_action_request",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_action_revisions"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            name="uq_action_revision_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            "payload_hash",
            name="uq_action_revision_payload",
        ),
    )
    op.create_table(
        "action_policy_decisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('allow', 'deny', 'require_approval')", name="outcome_valid"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "process_instance_id", "action_request_id", "revision"],
            [
                "action_revisions.tenant_id",
                "action_revisions.process_instance_id",
                "action_revisions.action_request_id",
                "action_revisions.revision",
            ],
            name="fk_action_policy_decisions_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_action_policy_decisions"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            name="uq_action_policy_decision_revision",
        ),
    )
    op.create_table(
        "approval_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("process_instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("required_role", sa.String(100)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled', 'superseded')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "process_instance_id",
                "action_request_id",
                "revision",
                "payload_hash",
            ],
            [
                "action_revisions.tenant_id",
                "action_revisions.process_instance_id",
                "action_revisions.action_request_id",
                "action_revisions.revision",
                "action_revisions.payload_hash",
            ],
            name="fk_approval_requests_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approval_requests"),
        sa.UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "action_request_id",
            "revision",
            name="uq_approval_request_revision",
        ),
    )
    op.create_index(
        "ix_approval_requests_pending",
        "approval_requests",
        ["tenant_id", "status", "expires_at"],
    )

    for table in (
        "action_requests",
        "action_revisions",
        "action_policy_decisions",
        "approval_requests",
    ):
        _enable_tenant_rls(table)

    op.execute(
        sa.text(
            """
            DO $grant_runtime$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    GRANT SELECT, INSERT
                        ON action_requests, action_revisions,
                           action_policy_decisions, approval_requests
                        TO tiramisu_app;
                    GRANT UPDATE
                        ON action_requests, approval_requests
                        TO tiramisu_app;
                END IF;
            END
            $grant_runtime$
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_approval_requests_pending", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_table("action_policy_decisions")
    op.drop_table("action_revisions")
    op.drop_index("ix_action_requests_process_status", table_name="action_requests")
    op.drop_table("action_requests")
