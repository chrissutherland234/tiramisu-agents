"""Dead-letter exhaustion, inspection, and attributed requeue integration tests."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.core.contracts.events import CanonicalEvent, ExternalReference
from tiramisu_agents.db.models.events import (
    EventInbox,
    ExternalCorrelation,
    OutboxMessage,
    OutboxRecoveryCommand,
)
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context
from tiramisu_agents.events.ingestion import EventIngestionService, ProcessBootstrap
from tiramisu_agents.security.credential_service import TenantCredentialService
from tiramisu_agents.security.credentials import CredentialScope
from tiramisu_agents.temporal.dispatcher import DispatchStatus, TemporalOutboxDispatcher

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


class _FailingTemporalClient:
    async def start_workflow(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("simulated Temporal outage")


class _SuccessfulTemporalClient:
    async def start_workflow(self, *_: Any, **__: Any) -> None:
        return None


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


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


async def _delete_tenant_data(
    admin_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> None:
    async with admin_factory.begin() as session:
        for model in (
            OutboxRecoveryCommand,
            OutboxMessage,
            EventInbox,
            ExternalCorrelation,
            TenantCredential,
            ProcessInstance,
        ):
            await session.execute(delete(model).where(model.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_dead_letter_can_be_inspected_and_idempotently_requeued() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    actor_id = uuid4()
    app = create_app(
        settings=_settings(
            environment="production",
            database_url=runtime_url,
            migration_database_url=migration_url,
        ),
        session_factory=runtime_factory,
    )

    try:
        async with admin_factory.begin() as session:
            session.add_all(
                [
                    Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Recovery"),
                    Tenant(
                        id=other_tenant_id,
                        slug=f"tenant-{other_tenant_id}",
                        name="Other recovery tenant",
                    ),
                ]
            )
        credential_service = TenantCredentialService()
        async with admin_factory.begin() as session:
            reader = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="dead-letter reader",
                scopes=(CredentialScope.OUTBOX_READ,),
            )
            operator = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="dead-letter operator",
                scopes=(CredentialScope.OUTBOX_READ, CredentialScope.OUTBOX_REQUEUE),
            )
            other_operator = await credential_service.issue(
                session,
                tenant_id=other_tenant_id,
                actor_id=uuid4(),
                name="other dead-letter operator",
                scopes=(CredentialScope.OUTBOX_READ, CredentialScope.OUTBOX_REQUEUE),
            )
        async with runtime_factory.begin() as session:
            ingested = await EventIngestionService().ingest(
                session,
                CanonicalEvent(
                    tenant_id=tenant_id,
                    event_type="enquiry.created",
                    source="outbox-recovery.test",
                    source_event_id=f"event-{uuid4()}",
                    occurred_at=datetime.now(UTC),
                    external_references=(
                        ExternalReference(
                            provider="outbox-recovery.test",
                            resource_type="enquiry",
                            external_id=f"enquiry-{uuid4()}",
                        ),
                    ),
                ),
                bootstrap=ProcessBootstrap(
                    process_type="enquiry_to_booking",
                    definition_version="1",
                    extension_manifest_hash="a" * 64,
                ),
            )
        assert ingested.outbox_message_id is not None
        message_id = ingested.outbox_message_id
        failing = TemporalOutboxDispatcher(
            runtime_factory,
            cast(Client, _FailingTemporalClient()),
            task_queue="outbox-recovery-test",
            max_attempts=2,
            retry_base_delay=timedelta(0),
        )
        first = await failing.dispatch_one(tenant_id)
        exhausted = await failing.dispatch_one(tenant_id)
        assert first.status is DispatchStatus.RETRY_SCHEDULED
        assert exhausted.status is DispatchStatus.DEAD_LETTERED

        transport = ASGITransport(app=app)
        reader_headers = {"Authorization": f"Bearer {reader.token}"}
        operator_headers = {"Authorization": f"Bearer {operator.token}"}
        other_headers = {"Authorization": f"Bearer {other_operator.token}"}
        command_id = uuid4()
        request_body = {
            "command_id": str(command_id),
            "reason": "Temporal connectivity has been restored",
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/v1/outbox/dead-letters", headers=reader_headers)
            assert listed.status_code == 200
            assert listed.json() == [
                {
                    "id": str(message_id),
                    "process_instance_id": str(ingested.process_instance_id),
                    "message_type": "temporal.process_event",
                    "destination": f"tenant/{tenant_id}/process/{ingested.process_instance_id}",
                    "attempt_count": 2,
                    "last_error": "RuntimeError: simulated Temporal outage",
                    "dead_lettered_at": listed.json()[0]["dead_lettered_at"],
                    "created_at": listed.json()[0]["created_at"],
                }
            ]
            forbidden = await client.post(
                f"/v1/outbox/dead-letters/{message_id}/requeue",
                headers=reader_headers,
                json=request_body,
            )
            assert forbidden.status_code == 403
            assert (await client.get("/v1/outbox/dead-letters", headers=other_headers)).json() == []
            hidden = await client.post(
                f"/v1/outbox/dead-letters/{message_id}/requeue",
                headers=other_headers,
                json={"reason": "Attempt to cross the tenant boundary"},
            )
            assert hidden.status_code == 409
            assert hidden.json()["detail"] == "dead-lettered outbox message not found"

            requeued, repeated = await asyncio.gather(
                client.post(
                    f"/v1/outbox/dead-letters/{message_id}/requeue",
                    headers=operator_headers,
                    json=request_body,
                ),
                client.post(
                    f"/v1/outbox/dead-letters/{message_id}/requeue",
                    headers=operator_headers,
                    json=request_body,
                ),
            )
            assert requeued.status_code == 202
            assert requeued.json() == {
                "command_id": str(command_id),
                "outbox_message_id": str(message_id),
                "status": "pending",
            }
            assert repeated.status_code == 202
            assert repeated.json() == requeued.json()
            assert (
                await client.get("/v1/outbox/dead-letters", headers=operator_headers)
            ).json() == []
            reused = await client.post(
                f"/v1/outbox/dead-letters/{message_id}/requeue",
                headers=operator_headers,
                json={"command_id": str(command_id), "reason": "A different command"},
            )
            assert reused.status_code == 409
            history = await client.get(
                f"/v1/outbox/recovery-commands?outbox_message_id={message_id}",
                headers=operator_headers,
            )
            assert history.status_code == 200
            assert history.json()[0] == {
                "id": str(command_id),
                "outbox_message_id": str(message_id),
                "actor_id": str(actor_id),
                "command_type": "requeue",
                "reason": "Temporal connectivity has been restored",
                "previous_attempt_count": 2,
                "previous_error": "RuntimeError: simulated Temporal outage",
                "previous_dead_lettered_at": history.json()[0]["previous_dead_lettered_at"],
                "created_at": history.json()[0]["created_at"],
            }

        delivered = await TemporalOutboxDispatcher(
            runtime_factory,
            cast(Client, _SuccessfulTemporalClient()),
            task_queue="outbox-recovery-test",
        ).dispatch_one(tenant_id)
        assert delivered.status is DispatchStatus.PUBLISHED

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            not_dead_lettered = await client.post(
                f"/v1/outbox/dead-letters/{message_id}/requeue",
                headers=operator_headers,
                json={"reason": "Do not requeue an already-published message"},
            )
            assert not_dead_lettered.status_code == 409
            assert not_dead_lettered.json()["detail"] == "outbox message is not dead-lettered"

        async with runtime_factory.begin() as session:
            await set_tenant_context(session, tenant_id)
            message = await session.get(OutboxMessage, message_id)
            recovery = await session.get(OutboxRecoveryCommand, command_id)
        assert message is not None
        assert recovery is not None
        assert message.status == "published"
        assert message.attempt_count == 1
        assert message.dead_lettered_at is None
        assert recovery.previous_attempt_count == 2
        assert recovery.previous_error == "RuntimeError: simulated Temporal outage"
        assert recovery.actor_id == actor_id
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await _delete_tenant_data(admin_factory, other_tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
