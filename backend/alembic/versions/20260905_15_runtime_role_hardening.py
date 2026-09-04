"""Normalize the least-privilege runtime database role.

Revision ID: 20260905_15
Revises: 20260902_14
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260905_15"
down_revision: str | None = "20260902_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPLICATION_TABLES = (
    "tenants",
    "process_instances",
    "tenant_credentials",
    "tenant_deployment_events",
    "tenant_safety_events",
    "action_requests",
    "event_inbox",
    "external_correlations",
    "process_control_commands",
    "process_interventions",
    "process_state_revisions",
    "action_revisions",
    "outbox_messages",
    "action_attempts",
    "action_policy_decisions",
    "approval_requests",
    "outbox_recovery_commands",
    "action_reconciliation_decisions",
    "approval_decisions",
    "review_threads",
    "review_messages",
)

_INSERT_TABLES = tuple(
    table
    for table in _APPLICATION_TABLES
    if table
    not in {
        "tenants",
        "tenant_credentials",
        "tenant_deployment_events",
        "tenant_safety_events",
    }
)

_UPDATE_COLUMNS = {
    "process_instances": (
        "status",
        "authoritative_facts",
        "customer_claims",
        "fact_provenance",
        "memory_summary",
        "memory_summary_source_event_ids",
        "memory_summary_source_review_command_ids",
        "memory_summary_source_action_attempt_ids",
        "memory_summary_source_timer_ids",
        "open_commitments",
        "current_wake_conditions",
        "state_version",
        "updated_at",
    ),
    "event_inbox": (
        "process_instance_id",
        "correlation_status",
        "correlation_reason",
        "updated_at",
    ),
    "outbox_messages": (
        "status",
        "available_at",
        "attempt_count",
        "claimed_at",
        "claim_token",
        "last_error",
        "published_at",
        "dead_lettered_at",
        "updated_at",
    ),
    "action_requests": ("current_revision", "status", "updated_at"),
    "approval_requests": ("status", "required_role", "expires_at", "updated_at"),
    "review_threads": ("status", "updated_at"),
    "action_attempts": (
        "status",
        "provider_reference",
        "result",
        "conflict",
        "facts",
        "error",
        "completed_at",
        "updated_at",
    ),
    "process_interventions": (
        "status",
        "resolved_by_command_id",
        "resolved_at",
        "updated_at",
    ),
}

_LEGACY_UPDATE_TABLES = (*_UPDATE_COLUMNS, "external_correlations")

_LEGACY_DELETE_TABLES = (
    "process_instances",
    "external_correlations",
    "event_inbox",
    "outbox_messages",
)


def _table_list(tables: Sequence[str]) -> str:
    return ", ".join(f'"{table}"' for table in tables)


def _role_statements(*, restore_legacy_delete: bool) -> str:
    select_tables = (*_APPLICATION_TABLES, "alembic_version")
    legacy_delete = (
        f"GRANT DELETE ON {_table_list(_LEGACY_DELETE_TABLES)} TO tiramisu_app;"
        if restore_legacy_delete
        else ""
    )
    selected_tables = select_tables if restore_legacy_delete else _APPLICATION_TABLES
    update_grants = (
        f"GRANT UPDATE ON {_table_list(_LEGACY_UPDATE_TABLES)} TO tiramisu_app;"
        if restore_legacy_delete
        else "\n".join(
            f'GRANT UPDATE ({_table_list(columns)}) ON "{table}" TO tiramisu_app;'
            for table, columns in _UPDATE_COLUMNS.items()
        )
    )
    return f"""
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM tiramisu_app;
        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM tiramisu_app;
        REVOKE ALL PRIVILEGES ON SCHEMA public FROM tiramisu_app;
        GRANT USAGE ON SCHEMA public TO tiramisu_app;
        GRANT SELECT ON {_table_list(selected_tables)} TO tiramisu_app;
        GRANT INSERT ON {_table_list(_INSERT_TABLES)} TO tiramisu_app;
        {update_grants}
        {legacy_delete}
    """


def _apply_role_contract(*, restore_legacy_delete: bool) -> None:
    statements = _role_statements(restore_legacy_delete=restore_legacy_delete)
    op.execute(
        sa.text(
            f"""
            DO $runtime_role$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tiramisu_app') THEN
                    {statements}
                END IF;
            END
            $runtime_role$
            """
        )
    )


def upgrade() -> None:
    _apply_role_contract(restore_legacy_delete=False)


def downgrade() -> None:
    _apply_role_contract(restore_legacy_delete=True)
