"""Data-bearing migration compatibility checks against an isolated PostgreSQL database."""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.db.models.actions import ActionAttempt, ActionRequest, ActionRevision
from tiramisu_agents.db.models.events import EventInbox, EventResolutionCommand, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.events.ingestion import EventIngestionService
from tiramisu_agents.events.quarantine import QuarantineResolutionService, ResolveQuarantineInput

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the PostgreSQL migration role",
)

_REPOSITORY_ROOT = Path(__file__).parents[3]


async def _run_alembic(database_url: str, *arguments: str) -> None:
    environment = {**os.environ, "TIRAMISU_MIGRATION_DATABASE_URL": database_url}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *arguments,
        cwd=_REPOSITORY_ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        pytest.fail(output.decode("utf-8", errors="replace"))


@pytest.mark.asyncio
async def test_conflict_migration_downgrades_populated_terminal_rows() -> None:
    configured_url = make_url(
        os.getenv(
            "TIRAMISU_MIGRATION_DATABASE_URL",
            "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
        )
    )
    database_name = f"tiramisu_migration_{uuid4().hex}"
    maintenance_url = configured_url.set(database="postgres")
    temporary_url = configured_url.set(database=database_name)
    maintenance_engine = create_engine(maintenance_url.render_as_string(hide_password=False))
    temporary_engine = None

    try:
        async with maintenance_engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        rendered_url = temporary_url.render_as_string(hide_password=False)
        await _run_alembic(rendered_url, "upgrade", "head")
        temporary_engine = create_engine(rendered_url)
        factory = create_session_factory(temporary_engine)
        tenant_id = uuid4()
        process_id = uuid4()
        action_request_id = uuid4()
        attempt_id = uuid4()
        async with factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"migration-{tenant_id}",
                    name="Migration fixture",
                    deployment_id="migration-test",
                )
            )
            await session.flush()
            session.add(
                ProcessInstance(
                    id=process_id,
                    tenant_id=tenant_id,
                    process_type="migration_test",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                    client_pack_fingerprint="b" * 64,
                    process_definition_fingerprint="c" * 64,
                    deployment_id="migration-test",
                    deployment_release_fingerprint="d" * 64,
                    temporal_task_queue="migration-test-queue",
                    workflow_id=f"process-{process_id}",
                )
            )
            await session.flush()
            session.add(
                ActionRequest(
                    id=action_request_id,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    agent_turn_id=uuid4(),
                    logical_action_key="conflicted_action",
                    action_type="propose_booking",
                    process_definition_version="1",
                    current_revision=1,
                    status="conflict",
                )
            )
            await session.flush()
            session.add(
                ActionRevision(
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    action_request_id=action_request_id,
                    revision=1,
                    parameters={"slot": "unavailable"},
                    payload_hash="e" * 64,
                    rationale="Migration fixture",
                    based_on_event_ids=[],
                    based_on_review_command_ids=[],
                    based_on_action_attempt_ids=[],
                    based_on_timer_ids=[],
                )
            )
            await session.flush()
            session.add(
                ActionAttempt(
                    id=attempt_id,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    action_request_id=action_request_id,
                    revision=1,
                    attempt_number=1,
                    idempotency_key="f" * 64,
                    adapter_id="migration.stub.v1",
                    status="conflict",
                    conflict={
                        "code": "resource_unavailable",
                        "message": "resource is unavailable",
                        "details": {},
                        "facts": [],
                    },
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
        await temporary_engine.dispose()
        temporary_engine = None

        await _run_alembic(rendered_url, "downgrade", "20260901_13")
        temporary_engine = create_engine(rendered_url)
        async with temporary_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT status FROM action_attempts WHERE id = :id"),
                    {"id": attempt_id},
                )
                == "failed"
            )
            assert (
                await connection.scalar(
                    text("SELECT status FROM action_requests WHERE id = :id"),
                    {"id": action_request_id},
                )
                == "failed"
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'action_attempts' AND column_name = 'conflict'"
                    )
                )
                == 0
            )
        await temporary_engine.dispose()
        temporary_engine = None

        await _run_alembic(rendered_url, "upgrade", "head")
        temporary_engine = create_engine(rendered_url)
        async with temporary_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT status FROM action_attempts WHERE id = :id"),
                    {"id": attempt_id},
                )
                == "failed"
            )
            assert (
                await connection.scalar(
                    select(ActionAttempt.conflict).where(ActionAttempt.id == attempt_id)
                )
                is None
            )
    finally:
        if temporary_engine is not None:
            await temporary_engine.dispose()
        async with maintenance_engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await maintenance_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_usage_schema", [False, True])
async def test_quarantine_migration_preserves_original_events_and_delivery_on_round_trip(
    legacy_usage_schema: bool,
) -> None:
    configured_url = make_url(
        os.getenv(
            "TIRAMISU_MIGRATION_DATABASE_URL",
            "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
        )
    )
    database_name = f"tiramisu_quarantine_migration_{uuid4().hex}"
    maintenance_engine = create_engine(
        configured_url.set(database="postgres").render_as_string(hide_password=False)
    )
    rendered_url = configured_url.set(database=database_name).render_as_string(hide_password=False)
    temporary_engine = None
    try:
        async with maintenance_engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        await _run_alembic(rendered_url, "upgrade", "20260905_17")
        temporary_engine = create_engine(rendered_url)
        factory = create_session_factory(temporary_engine)
        tenant_id, process_id, actor_id = uuid4(), uuid4(), uuid4()
        event = CanonicalEvent(
            tenant_id=tenant_id,
            event_type="customer.email_received",
            source="migration.test",
            source_event_id="original",
            occurred_at=datetime.now(UTC),
            payload={"body": "Preserve me"},
        )
        async with factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    slug=f"migration-{tenant_id}",
                    name="Migration",
                    deployment_id="migration-test",
                )
            )
            await session.flush()
            session.add(
                ProcessInstance(
                    id=process_id,
                    tenant_id=tenant_id,
                    process_type="migration_test",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                    client_pack_fingerprint="b" * 64,
                    process_definition_fingerprint="c" * 64,
                    deployment_id="migration-test",
                    deployment_release_fingerprint="d" * 64,
                    temporal_task_queue="migration-test-queue",
                    workflow_id=f"process-{process_id}",
                )
            )
            await EventIngestionService().ingest(session, event)
            if legacy_usage_schema:
                await session.execute(
                    text("ALTER TABLE model_usage_ledger DROP COLUMN execution_id CASCADE")
                )
                await session.execute(
                    text(
                        "ALTER TABLE model_usage_ledger ADD CONSTRAINT uq_model_usage_turn_attempt "
                        "UNIQUE (tenant_id, process_instance_id, agent_turn_id, attempt_number)"
                    )
                )
            await session.execute(
                text(
                    "INSERT INTO model_usage_ledger "
                    "(tenant_id, process_instance_id, agent_turn_id, attempt_number, model, "
                    "input_tokens, output_tokens, cost_micros, price_table_version) "
                    "VALUES (:tenant, :process, :turn, 1, 'test', 123, 45, 67, 1)"
                ),
                {"tenant": tenant_id, "process": process_id, "turn": uuid4()},
            )
        await temporary_engine.dispose()
        await _run_alembic(rendered_url, "upgrade", "head")
        async with factory.begin() as session:
            stored = await QuarantineResolutionService().resolve(
                session,
                ResolveQuarantineInput(
                    command_id=uuid4(),
                    tenant_id=tenant_id,
                    event_id=event.event_id,
                    process_instance_id=process_id,
                    actor_id=actor_id,
                    reason="Verify upgrade",
                ),
            )
            assert stored.delivery_scheduled
        await temporary_engine.dispose()
        await _run_alembic(rendered_url, "downgrade", "20260905_17")
        await _run_alembic(rendered_url, "upgrade", "head")
        await _run_alembic(rendered_url, "check")
        async with factory.begin() as session:
            inbox = await session.get(EventInbox, event.event_id)
            assert inbox is not None and inbox.event_data == event.model_dump(mode="json")
            assert inbox.process_instance_id == process_id and inbox.correlation_status == "matched"
            delivery = await session.scalar(
                select(OutboxMessage).where(OutboxMessage.causation_event_id == event.event_id)
            )
            assert delivery is not None and delivery.status == "pending"
            usage = (
                await session.execute(
                    text(
                        "SELECT input_tokens, output_tokens, cost_micros, execution_id "
                        "FROM model_usage_ledger"
                    )
                )
            ).one()
            assert tuple(usage[:3]) == (123, 45, 67)
            assert str(usage.execution_id) == "00000000-0000-0000-0000-000000000000"
            # Downgrade intentionally removes this additive audit table, not the event or outbox.
            assert (
                await session.scalar(select(func.count()).select_from(EventResolutionCommand)) == 0
            )
    finally:
        if temporary_engine is not None:
            await temporary_engine.dispose()
        async with maintenance_engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await maintenance_engine.dispose()
