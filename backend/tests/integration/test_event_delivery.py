"""End-to-end canonical event persistence and Temporal outbox delivery."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import (
    create_engine,
    create_session_factory,
    set_tenant_context,
)
from tiramisu_agents.events.ingestion import (
    EventIngestionService,
    IngestionResult,
    ProcessBootstrap,
)
from tiramisu_agents.temporal.dispatcher import DispatchStatus, TemporalOutboxDispatcher
from tiramisu_agents.temporal.workflows.mailbox import ProcessMailboxWorkflow

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


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


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    # Kept inline in tests because tenant deletion is a control-plane operation.
    async with admin_factory.begin() as session:
        await session.execute(delete(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id))
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(delete(ProcessInstance).where(ProcessInstance.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_ingestion_deduplicates_and_quarantines_unmatched_events() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    service = EventIngestionService()
    reference = ExternalReference(
        provider="stub.website", resource_type="enquiry", external_id=f"enquiry-{uuid4()}"
    )
    event = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="stub.website",
        source_event_id=f"source-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(reference,),
        payload={"message": "Synthetic integration enquiry"},
    )

    try:
        async with admin_factory.begin() as session:
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Test Tenant"))

        async with runtime_factory.begin() as session:
            created = await service.ingest(
                session,
                event,
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                ),
            )
        assert created.created is True
        assert created.correlation_status == "matched"
        assert created.process_instance_id is not None
        assert created.outbox_message_id is not None

        duplicate = event.model_copy(update={"event_id": uuid4()})
        async with runtime_factory.begin() as session:
            deduplicated = await service.ingest(session, duplicate)
        assert deduplicated.created is False
        assert deduplicated.event_id == event.event_id
        assert deduplicated.outbox_message_id == created.outbox_message_id

        unmatched = CanonicalEvent(
            tenant_id=tenant_id,
            event_type="payment.completed",
            source="stub.payments",
            source_event_id=f"payment-{uuid4()}",
            occurred_at=datetime.now(UTC),
            external_references=(
                ExternalReference(
                    provider="stub.payments",
                    resource_type="payment",
                    external_id=f"payment-{uuid4()}",
                ),
            ),
        )
        async with runtime_factory.begin() as session:
            quarantined = await service.ingest(session, unmatched)
        assert quarantined.correlation_status == "pending"
        assert quarantined.correlation_reason == "no_process_match"
        assert quarantined.outbox_message_id is None
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_development_api_can_start_a_configured_process() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    app = create_app(
        settings=Settings(
            database_url=runtime_url,
            migration_database_url=migration_url,
            allow_unsafe_development_tenant_header=True,
            load_fictional_example_processes=True,
        ),
        session_factory=runtime_factory,
    )

    try:
        async with admin_factory.begin() as session:
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Test Tenant"))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/events",
                headers={"X-Tiramisu-Tenant-ID": str(tenant_id)},
                json={
                    "event_type": "enquiry.created",
                    "source": "stub.website",
                    "source_event_id": f"source-{uuid4()}",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "external_references": [
                        {
                            "provider": "stub.website",
                            "resource_type": "enquiry",
                            "external_id": f"enquiry-{uuid4()}",
                        }
                    ],
                    "payload": {"message": "Synthetic API enquiry"},
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["created"] is True
        assert body["correlation_status"] == "matched"
        assert body["delivery_scheduled"] is True
        assert body["process_instance_id"] is not None
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_trigger_delivery_creates_one_process() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    source_event_id = f"source-{uuid4()}"
    reference = ExternalReference(
        provider="stub.website", resource_type="enquiry", external_id=f"enquiry-{uuid4()}"
    )
    base_event = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="stub.website",
        source_event_id=source_event_id,
        occurred_at=datetime.now(UTC),
        external_references=(reference,),
    )
    bootstrap = ProcessBootstrap(
        process_type="enquiry_to_booking",
        definition_version="1",
        extension_manifest_hash="a" * 64,
    )

    async def ingest(event: CanonicalEvent) -> IngestionResult:
        async with runtime_factory.begin() as session:
            return await EventIngestionService().ingest(session, event, bootstrap=bootstrap)

    try:
        async with admin_factory.begin() as session:
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Test Tenant"))

        first, second = await asyncio.gather(
            ingest(base_event),
            ingest(base_event.model_copy(update={"event_id": uuid4()})),
        )
        assert {first.created, second.created} == {True, False}
        assert first.process_instance_id == second.process_instance_id

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            process_count = await session.scalar(select(func.count()).select_from(ProcessInstance))
            outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))
        assert process_count == 1
        assert outbox_count == 1
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_outbox_signal_with_start_is_safe_to_redeliver() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    event = CanonicalEvent(
        tenant_id=tenant_id,
        event_type="enquiry.created",
        source="stub.website",
        source_event_id=f"source-{uuid4()}",
        occurred_at=datetime.now(UTC),
        external_references=(
            ExternalReference(
                provider="stub.website",
                resource_type="enquiry",
                external_id=f"enquiry-{uuid4()}",
            ),
        ),
    )
    workflow_id: str | None = None

    try:
        async with admin_factory.begin() as session:
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Test Tenant"))
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                event,
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                ),
            )
            workflow_id = await session.scalar(
                select(ProcessInstance.workflow_id).where(
                    ProcessInstance.id == ingested.process_instance_id
                )
            )
        assert workflow_id is not None

        task_queue = f"delivery-test-{uuid4()}"
        async with (
            await WorkflowEnvironment.start_time_skipping() as environment,
            Worker(
                environment.client,
                task_queue=task_queue,
                workflows=[ProcessMailboxWorkflow],
            ),
        ):
            dispatcher = TemporalOutboxDispatcher(
                runtime_factory, environment.client, task_queue=task_queue
            )
            first = await dispatcher.dispatch_one(tenant_id)
            assert first.status is DispatchStatus.PUBLISHED
            assert first.message_id is not None

            handle = environment.client.get_workflow_handle(workflow_id)
            state = await handle.query(ProcessMailboxWorkflow.state)
            assert state.buffered_events == ()
            assert [wake.reason for wake in state.wake_records] == ["process_started"]
            assert state.wake_records[0].event_id == str(event.event_id)

            async with runtime_factory.begin() as session:
                await set_tenant_context(session, tenant_id)
                stored = await session.get(OutboxMessage, first.message_id)
                assert stored is not None
                stored.status = "pending"
                stored.published_at = None

            second = await dispatcher.dispatch_one(tenant_id)
            assert second.status is DispatchStatus.PUBLISHED
            state = await handle.query(ProcessMailboxWorkflow.state)
            assert state.buffered_events == ()
            assert [wake.reason for wake in state.wake_records] == ["process_started"]
            await handle.signal(ProcessMailboxWorkflow.close)
            await handle.result()
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
