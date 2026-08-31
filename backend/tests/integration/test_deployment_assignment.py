"""Audited tenant deployment assignment and safe logical-deployment moves."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant, TenantDeploymentEvent
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.security.deployment_assignment import (
    TenantDeploymentConflict,
    TenantDeploymentService,
)
from tiramisu_agents.security.deployment_lock import lock_tenant_deployment_for_ingress
from tiramisu_agents.testkit import TEST_DEPLOYMENT_RELEASE

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


def _database_urls() -> tuple[str, str]:
    return (
        os.getenv(
            "TIRAMISU_DATABASE_URL",
            "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test",
        ),
        os.getenv(
            "TIRAMISU_MIGRATION_DATABASE_URL",
            "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
        ),
    )


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        await session.execute(
            delete(TenantDeploymentEvent).where(TenantDeploymentEvent.tenant_id == tenant_id)
        )
        await session.execute(delete(OutboxMessage).where(OutboxMessage.tenant_id == tenant_id))
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(delete(ProcessInstance).where(ProcessInstance.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_assignment_is_audited_idempotent_and_refuses_to_strand_work() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    actor_id = uuid4()
    service = TenantDeploymentService()

    try:
        async with admin_factory.begin() as session:
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Deployment test"))

        async with admin_factory.begin() as session:
            assigned = await service.assign(
                session,
                tenant_id=tenant_id,
                deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                actor_id=actor_id,
                reason="Install the first managed client pack",
            )
        assert assigned.changed is True
        assert assigned.previous_deployment_id == "unassigned"
        assert assigned.event_id is not None

        async with admin_factory.begin() as session:
            repeated = await service.assign(
                session,
                tenant_id=tenant_id,
                deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                actor_id=actor_id,
                reason="Retry the same control-plane command",
            )
        assert repeated.changed is False
        assert repeated.event_id is None

        async def attempt_concurrent_move() -> object:
            async with admin_factory.begin() as session:
                return await service.assign(
                    session,
                    tenant_id=tenant_id,
                    deployment_id="replacement-deployment",
                    actor_id=actor_id,
                    reason="Unsafe move racing an initiating event",
                )

        async with runtime_factory.begin() as session:
            await lock_tenant_deployment_for_ingress(session, tenant_id)
            pending_move = asyncio.create_task(attempt_concurrent_move())
            await asyncio.sleep(0.05)
            assert pending_move.done() is False
            ingested = await EventIngestionService().ingest(
                session,
                CanonicalEvent(
                    tenant_id=tenant_id,
                    event_type="enquiry.created",
                    source="deployment.test",
                    source_event_id=f"event-{uuid4()}",
                    occurred_at=datetime.now(UTC),
                    external_references=(
                        ExternalReference(
                            provider="deployment.test",
                            resource_type="enquiry",
                            external_id=f"enquiry-{uuid4()}",
                        ),
                    ),
                ),
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                    client_pack_fingerprint=TEST_DEPLOYMENT_RELEASE.client_pack_fingerprint,
                    process_definition_fingerprint="c" * 64,
                    deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
                    deployment_release_fingerprint=(TEST_DEPLOYMENT_RELEASE.release_fingerprint),
                    temporal_task_queue=TEST_DEPLOYMENT_RELEASE.temporal_task_queue,
                ),
                deployment_id=TEST_DEPLOYMENT_RELEASE.deployment_id,
            )
        assert ingested.process_instance_id is not None
        assert ingested.outbox_message_id is not None
        with pytest.raises(TenantDeploymentConflict, match="active processes"):
            await pending_move

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            process = await session.get(ProcessInstance, ingested.process_instance_id)
            assert process is not None
            process.status = "completed"

        with pytest.raises(TenantDeploymentConflict, match="outstanding deliveries"):
            async with admin_factory.begin() as session:
                await service.assign(
                    session,
                    tenant_id=tenant_id,
                    deployment_id="replacement-deployment",
                    actor_id=actor_id,
                    reason="Unsafe move with an undelivered terminal message",
                )

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            message = await session.get(OutboxMessage, ingested.outbox_message_id)
            assert message is not None
            message.status = "published"
            message.published_at = datetime.now(UTC)

        async with admin_factory.begin() as session:
            moved = await service.assign(
                session,
                tenant_id=tenant_id,
                deployment_id="replacement-deployment",
                actor_id=actor_id,
                reason="All previous work is terminal and fully delivered",
            )
        assert moved.changed is True
        assert moved.previous_deployment_id == TEST_DEPLOYMENT_RELEASE.deployment_id

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            tenant = await session.get(Tenant, tenant_id)
            events = list(
                await session.scalars(
                    select(TenantDeploymentEvent)
                    .where(TenantDeploymentEvent.tenant_id == tenant_id)
                    .order_by(TenantDeploymentEvent.created_at)
                )
            )
        assert tenant is not None
        assert tenant.deployment_id == "replacement-deployment"
        assert [event.new_deployment_id for event in events] == [
            TEST_DEPLOYMENT_RELEASE.deployment_id,
            "replacement-deployment",
        ]
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
