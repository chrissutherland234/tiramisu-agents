"""Exhaustive PostgreSQL tenant-isolation and runtime-role checks."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.events import (
    EventInbox,
    ExternalCorrelation,
    OutboxMessage,
    OutboxRecoveryCommand,
)
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import (
    Tenant,
    TenantCredential,
    TenantDeploymentEvent,
    TenantSafetyEvent,
)
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)

_TENANT_TABLES = (
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

_CONTROL_PLANE_READ_ONLY_TABLES = frozenset(
    {
        "tenants",
        "tenant_credentials",
        "tenant_deployment_events",
        "tenant_safety_events",
    }
)

_UPDATE_COLUMNS = {
    "process_instances": frozenset(
        {
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
        }
    ),
    "event_inbox": frozenset(
        {"process_instance_id", "correlation_status", "correlation_reason", "updated_at"}
    ),
    "outbox_messages": frozenset(
        {
            "status",
            "available_at",
            "attempt_count",
            "claimed_at",
            "claim_token",
            "last_error",
            "published_at",
            "dead_lettered_at",
            "updated_at",
        }
    ),
    "action_requests": frozenset({"current_revision", "status", "updated_at"}),
    "approval_requests": frozenset({"status", "required_role", "expires_at", "updated_at"}),
    "review_threads": frozenset({"status", "updated_at"}),
    "action_attempts": frozenset(
        {
            "status",
            "provider_reference",
            "result",
            "conflict",
            "facts",
            "error",
            "completed_at",
            "updated_at",
        }
    ),
    "process_interventions": frozenset(
        {"status", "resolved_by_command_id", "resolved_at", "updated_at"}
    ),
}


def _expected_grants(table: str) -> frozenset[str]:
    privileges: set[str] = {"SELECT"}
    if table not in _CONTROL_PLANE_READ_ONLY_TABLES:
        privileges.add("INSERT")
    return frozenset(privileges)


_EXPECTED_GRANTS = {table: _expected_grants(table) for table in _TENANT_TABLES}


def _database_urls() -> tuple[str, str]:
    runtime = os.getenv(
        "TIRAMISU_DATABASE_URL",
        "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test",
    )
    migration = os.getenv(
        "TIRAMISU_MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
    )
    return runtime, migration


async def _seed_tenant_graph(session: AsyncSession, tenant_id: UUID) -> None:
    now = datetime.now(UTC)
    process_id = uuid4()
    event_id = uuid4()
    outbox_id = uuid4()
    action_request_id = uuid4()
    action_attempt_id = uuid4()
    approval_request_id = uuid4()
    review_thread_id = uuid4()
    intervention_id = uuid4()
    actor_id = uuid4()
    payload_hash = uuid4().hex * 2

    session.add(
        Tenant(
            id=tenant_id,
            slug=f"rls-{tenant_id}",
            name=f"RLS fixture {tenant_id}",
            deployment_id="rls-audit",
        )
    )
    await session.flush()
    session.add_all(
        (
            ProcessInstance(
                id=process_id,
                tenant_id=tenant_id,
                process_type="rls_audit",
                definition_version="1",
                extension_manifest_hash="a" * 64,
                client_pack_fingerprint="b" * 64,
                process_definition_fingerprint="c" * 64,
                deployment_id="rls-audit",
                deployment_release_fingerprint="d" * 64,
                temporal_task_queue=f"rls-audit-{tenant_id}",
                status="waiting",
                workflow_id=f"rls-audit-{tenant_id}",
            ),
            TenantCredential(
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="RLS audit credential",
                secret_hash="e" * 64,
                scopes=[],
                roles=[],
                status="active",
            ),
            TenantDeploymentEvent(
                tenant_id=tenant_id,
                actor_id=actor_id,
                previous_deployment_id="unassigned",
                new_deployment_id="rls-audit",
                reason="Exercise tenant isolation",
            ),
            TenantSafetyEvent(
                tenant_id=tenant_id,
                actor_id=actor_id,
                previous_status="active",
                new_status="suspended",
                reason="Exercise tenant isolation",
                metadata_={},
            ),
        )
    )
    await session.flush()
    session.add_all(
        (
            EventInbox(
                id=event_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                source="rls.audit",
                source_event_id=f"event-{tenant_id}",
                event_type="audit.started",
                event_data={},
                correlation_status="matched",
                received_at=now,
            ),
            ExternalCorrelation(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                provider="rls.audit",
                resource_type="case",
                external_id=str(tenant_id),
            ),
            ActionRequest(
                id=action_request_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=uuid4(),
                logical_action_key="rls_audit",
                action_type="audit_action",
                process_definition_version="1",
                current_revision=1,
                status="pending_approval",
            ),
            ProcessStateRevision(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=uuid4(),
                decision_id=uuid4(),
                version=1,
                decision_status="waiting",
                process_status="waiting",
                authoritative_facts={},
                customer_claims={},
                fact_provenance={},
                memory_summary=None,
                memory_summary_source_event_ids=[],
                memory_summary_source_review_command_ids=[],
                memory_summary_source_action_attempt_ids=[],
                memory_summary_source_timer_ids=[],
                open_commitments=[],
                wake_conditions=[],
                based_on_event_ids=[str(event_id)],
                based_on_review_command_ids=[],
                based_on_action_attempt_ids=[],
                based_on_timer_ids=[],
            ),
            ProcessIntervention(
                id=intervention_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                agent_turn_id=uuid4(),
                kind="turn_failure",
                status="open",
                error_type="RlsAudit",
                error="Exercise tenant isolation",
                source_event_ids=[str(event_id)],
                source_review_command_ids=[],
                source_action_attempt_ids=[],
                source_timer_ids=[],
            ),
            ProcessControlCommand(
                id=uuid4(),
                tenant_id=tenant_id,
                process_instance_id=process_id,
                actor_id=actor_id,
                intervention_id=intervention_id,
                command_type="takeover",
                reason="Exercise tenant isolation",
                payload={},
            ),
        )
    )
    await session.flush()
    session.add_all(
        (
            OutboxMessage(
                id=outbox_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                causation_event_id=event_id,
                message_type="temporal.process_event",
                destination=f"tenant/{tenant_id}/process/{process_id}",
                deduplication_key=f"rls-audit-{tenant_id}",
                payload={},
                status="dead_letter",
                attempt_count=1,
                dead_lettered_at=now,
            ),
            ActionRevision(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                action_request_id=action_request_id,
                revision=1,
                parameters={},
                payload_hash=payload_hash,
                rationale="Exercise tenant isolation",
                based_on_event_ids=[str(event_id)],
                based_on_review_command_ids=[],
                based_on_action_attempt_ids=[],
                based_on_timer_ids=[],
            ),
        )
    )
    await session.flush()
    session.add_all(
        (
            OutboxRecoveryCommand(
                id=uuid4(),
                tenant_id=tenant_id,
                outbox_message_id=outbox_id,
                actor_id=actor_id,
                command_type="requeue",
                reason="Exercise tenant isolation",
                previous_attempt_count=1,
                previous_error="RLS audit",
                previous_dead_lettered_at=now,
            ),
            ActionPolicyRecord(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                action_request_id=action_request_id,
                revision=1,
                outcome="require_approval",
                policy_version="rls-audit-v1",
                reason="Exercise tenant isolation",
            ),
            ApprovalRequest(
                id=approval_request_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                action_request_id=action_request_id,
                revision=1,
                payload_hash=payload_hash,
                status="pending",
            ),
            ActionAttempt(
                id=action_attempt_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                action_request_id=action_request_id,
                revision=1,
                attempt_number=1,
                idempotency_key=uuid4().hex * 2,
                adapter_id="rls.audit.v1",
                status="unknown",
                facts=[],
                started_at=now,
            ),
        )
    )
    await session.flush()
    session.add_all(
        (
            ActionReconciliationDecision(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                action_request_id=action_request_id,
                action_attempt_id=action_attempt_id,
                actor_id=actor_id,
                previous_status="unknown",
                resolution="succeeded",
                evidence="Exercise tenant isolation",
                result={},
            ),
            ReviewThread(
                id=review_thread_id,
                tenant_id=tenant_id,
                process_instance_id=process_id,
                approval_request_id=approval_request_id,
                status="open",
            ),
            ApprovalDecision(
                tenant_id=tenant_id,
                process_instance_id=process_id,
                approval_request_id=approval_request_id,
                actor_id=actor_id,
                decision="approved",
                payload_hash=payload_hash,
                reason="Exercise tenant isolation",
            ),
        )
    )
    await session.flush()
    session.add(
        ReviewMessage(
            tenant_id=tenant_id,
            process_instance_id=process_id,
            review_thread_id=review_thread_id,
            actor_id=actor_id,
            message_type="comment",
            content="Exercise tenant isolation",
            proposal_revision=1,
        )
    )


async def _delete_tenant_graph(
    admin_factory: async_sessionmaker[AsyncSession], tenant_ids: tuple[UUID, ...]
) -> None:
    parameters = {f"tenant_{index}": tenant_id for index, tenant_id in enumerate(tenant_ids)}
    placeholders = ", ".join(f":tenant_{index}" for index in range(len(tenant_ids)))
    async with admin_factory.begin() as session:
        for table in reversed(Base.metadata.sorted_tables):
            tenant_column = "id" if table.name == "tenants" else "tenant_id"
            await session.execute(
                text(f'DELETE FROM "{table.name}" WHERE "{tenant_column}" IN ({placeholders})'),
                parameters,
            )


@asynccontextmanager
async def _seeded_databases() -> AsyncGenerator[
    tuple[
        async_sessionmaker[AsyncSession],
        async_sessionmaker[AsyncSession],
        tuple[UUID, UUID],
    ]
]:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_ids = (uuid4(), uuid4())
    try:
        async with admin_factory.begin() as session:
            for tenant_id in tenant_ids:
                await _seed_tenant_graph(session, tenant_id)
        yield runtime_factory, admin_factory, tenant_ids
    finally:
        await _delete_tenant_graph(admin_factory, tenant_ids)
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_every_tenant_table_has_forced_rls_and_exact_runtime_grants() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    try:
        metadata_tables = {
            table.name
            for table in Base.metadata.sorted_tables
            if table.name == "tenants" or "tenant_id" in table.c
        }
        assert metadata_tables == set(_TENANT_TABLES)

        async with admin_engine.connect() as connection:
            policies = (
                (
                    await connection.execute(
                        text(
                            "SELECT c.relname AS table_name, c.relrowsecurity, "
                            "c.relforcerowsecurity, c.relowner::regrole::text AS owner, "
                            "p.polname, p.polcmd, p.polroles, "
                            "pg_get_expr(p.polqual, p.polrelid) AS using_expression, "
                            "pg_get_expr(p.polwithcheck, p.polrelid) AS check_expression "
                            "FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "LEFT JOIN pg_policy p ON p.polrelid = c.oid "
                            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                        )
                    )
                )
                .mappings()
                .all()
            )
            policies_by_table: dict[str, list[dict[str, object]]] = {}
            for row in policies:
                table_name = str(row["table_name"])
                if table_name in metadata_tables:
                    policies_by_table.setdefault(table_name, []).append(dict(row))

            assert set(policies_by_table) == metadata_tables
            for table_name, table_policies in policies_by_table.items():
                assert len(table_policies) == 1, table_name
                policy = table_policies[0]
                assert policy["relrowsecurity"] is True, table_name
                assert policy["relforcerowsecurity"] is True, table_name
                assert policy["owner"] != "tiramisu_app", table_name
                assert policy["polname"] == "tenant_isolation", table_name
                policy_command = policy["polcmd"]
                if isinstance(policy_command, bytes):
                    policy_command = policy_command.decode("ascii")
                assert policy_command == "*", table_name
                assert policy["polroles"] == [0], table_name
                using_expression = str(policy["using_expression"])
                assert using_expression == str(policy["check_expression"]), table_name
                assert "current_setting('app.tenant_id'::text, true)" in using_expression
                tenant_column = "id" if table_name == "tenants" else "tenant_id"
                assert f"({tenant_column} =" in using_expression, table_name

            grant_rows = (
                await connection.execute(
                    text(
                        "SELECT table_name, privilege_type "
                        "FROM information_schema.role_table_grants "
                        "WHERE grantee = 'tiramisu_app' AND table_schema = 'public'"
                    )
                )
            ).all()
            actual_grants: dict[str, set[str]] = {}
            for table_name, privilege in grant_rows:
                actual_grants.setdefault(str(table_name), set()).add(str(privilege))
            assert actual_grants == {
                table: set(privileges) for table, privileges in _EXPECTED_GRANTS.items()
            }

            update_rows = (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name "
                        "FROM information_schema.role_column_grants "
                        "WHERE grantee = 'tiramisu_app' AND table_schema = 'public' "
                        "AND privilege_type = 'UPDATE'"
                    )
                )
            ).all()
            actual_update_columns: dict[str, set[str]] = {}
            for table_name, column_name in update_rows:
                actual_update_columns.setdefault(str(table_name), set()).add(str(column_name))
            assert actual_update_columns == {
                table: set(columns) for table, columns in _UPDATE_COLUMNS.items()
            }

            role = (
                await connection.execute(
                    text(
                        "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
                        "rolreplication, rolbypassrls "
                        "FROM pg_roles WHERE rolname = 'tiramisu_app'"
                    )
                )
            ).one()
            assert tuple(role) == (True, False, False, False, False, False)
            schema_privileges = (
                await connection.execute(
                    text(
                        "SELECT has_schema_privilege('tiramisu_app', 'public', 'USAGE'), "
                        "has_schema_privilege('tiramisu_app', 'public', 'CREATE')"
                    )
                )
            ).one()
            assert tuple(schema_privileges) == (True, False)

        async with runtime_engine.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == "tiramisu_app"
            with pytest.raises(DBAPIError, match="permission denied"):
                await connection.execute(text("SELECT version_num FROM alembic_version"))
    finally:
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_every_tenant_table_filters_cross_tenant_rows_and_pool_context_resets() -> None:
    async with _seeded_databases() as (runtime_factory, _admin_factory, tenant_ids):
        tenant_a, tenant_b = tenant_ids
        first_backend_pid: int | None = None

        async with runtime_factory.begin() as session:
            first_backend_pid = await session.scalar(select(text("pg_backend_pid()")))
            await set_tenant_context(session, tenant_a)
            for table_name in _TENANT_TABLES:
                tenant_column = "id" if table_name == "tenants" else "tenant_id"
                visible = await session.scalar(
                    text(
                        f'SELECT count(*) FROM "{table_name}" WHERE "{tenant_column}" = :tenant_id'
                    ),
                    {"tenant_id": tenant_a},
                )
                hidden = await session.scalar(
                    text(
                        f'SELECT count(*) FROM "{table_name}" WHERE "{tenant_column}" = :tenant_id'
                    ),
                    {"tenant_id": tenant_b},
                )
                assert visible == 1, table_name
                assert hidden == 0, table_name

        # set_config(..., true) is transaction-local. The same pooled physical
        # connection must return with no residual tenant before being reassigned.
        async with runtime_factory.begin() as session:
            second_backend_pid = await session.scalar(select(text("pg_backend_pid()")))
            assert second_backend_pid == first_backend_pid
            assert await session.scalar(text("SELECT current_setting('app.tenant_id', true)")) in {
                None,
                "",
            }
            for table_name in _TENANT_TABLES:
                assert await session.scalar(text(f'SELECT count(*) FROM "{table_name}"')) == 0

            await set_tenant_context(session, tenant_b)
            for table_name in _TENANT_TABLES:
                tenant_column = "id" if table_name == "tenants" else "tenant_id"
                assert (
                    await session.scalar(
                        text(
                            f'SELECT count(*) FROM "{table_name}" '
                            f'WHERE "{tenant_column}" = :tenant_id'
                        ),
                        {"tenant_id": tenant_b},
                    )
                    == 1
                ), table_name


@pytest.mark.asyncio
async def test_runtime_role_cannot_cross_tenant_write_or_delete_audit_data() -> None:
    async with _seeded_databases() as (runtime_factory, _admin_factory, tenant_ids):
        tenant_a, tenant_b = tenant_ids
        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_a)
            changed = (
                (
                    await session.execute(
                        text(
                            "UPDATE process_instances SET status = status "
                            "WHERE tenant_id = :tenant_b RETURNING id"
                        ),
                        {"tenant_b": tenant_b},
                    )
                )
                .scalars()
                .all()
            )
            assert changed == []

        with pytest.raises(DBAPIError, match="row-level security"):
            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_a)
                await session.execute(
                    text(
                        "INSERT INTO process_instances "
                        "(id, tenant_id, process_type, definition_version, "
                        "extension_manifest_hash, client_pack_fingerprint, "
                        "process_definition_fingerprint, deployment_id, "
                        "deployment_release_fingerprint, temporal_task_queue, workflow_id) "
                        "VALUES (:id, :tenant_b, 'rls_audit', '1', :hash, :hash, :hash, "
                        "'rls-audit', :hash, 'rls-audit-cross-tenant', :workflow_id)"
                    ),
                    {
                        "id": uuid4(),
                        "tenant_b": tenant_b,
                        "hash": "f" * 64,
                        "workflow_id": f"rls-cross-tenant-{uuid4()}",
                    },
                )

        with pytest.raises(DBAPIError, match="permission denied"):
            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_a)
                await session.execute(
                    text("DELETE FROM process_instances WHERE tenant_id = :tenant_a"),
                    {"tenant_a": tenant_a},
                )

        with pytest.raises(DBAPIError, match="permission denied"):
            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_a)
                await session.execute(
                    text("UPDATE review_messages SET content = content WHERE tenant_id = :tenant"),
                    {"tenant": tenant_a},
                )

        with pytest.raises(DBAPIError, match="permission denied"):
            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_a)
                await session.execute(
                    text(
                        "UPDATE process_instances SET deployment_id = deployment_id "
                        "WHERE tenant_id = :tenant"
                    ),
                    {"tenant": tenant_a},
                )
