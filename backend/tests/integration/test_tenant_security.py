"""Production bearer authentication and auditable tenant safety controls."""

import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation
from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential, TenantSafetyEvent
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.security.credential_service import TenantCredentialService
from tiramisu_agents.security.credentials import CredentialScope
from tiramisu_agents.security.tenancy import SafetyControlConflict, TenantSafetyService

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


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
    async with admin_factory.begin() as session:
        await session.execute(delete(EventInbox).where(EventInbox.tenant_id == tenant_id))
        await session.execute(
            delete(ExternalCorrelation).where(ExternalCorrelation.tenant_id == tenant_id)
        )
        await session.execute(
            delete(TenantSafetyEvent).where(TenantSafetyEvent.tenant_id == tenant_id)
        )
        await session.execute(
            delete(TenantCredential).where(TenantCredential.tenant_id == tenant_id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_production_credentials_enforce_tenant_scope_lifecycle_and_suspension() -> None:
    runtime_url, migration_url = _database_urls()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(migration_url)
    runtime_factory = create_session_factory(runtime_engine)
    admin_factory = create_session_factory(admin_engine)
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    actor_id = uuid4()
    control_actor_id = uuid4()
    credential_service = TenantCredentialService()
    safety_service = TenantSafetyService()
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
            session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id}", name="Secure Tenant"))
        async with admin_factory.begin() as session:
            reader = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="operator reader",
                scopes=(CredentialScope.PROCESSES_READ,),
            )
            ingress = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="event ingress",
                scopes=(CredentialScope.EVENTS_INGEST,),
            )
            expiring = await credential_service.issue(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name="short-lived reader",
                scopes=(CredentialScope.PROCESSES_READ,),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            stored_expiring = await session.get(TenantCredential, expiring.credential_id)
            assert stored_expiring is not None
            stored_expiring.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            unauthenticated = await client.get("/v1/processes")
            assert unauthenticated.status_code == 401
            assert unauthenticated.headers["www-authenticate"] == "Bearer"

            invalid = await client.get(
                "/v1/processes", headers={"Authorization": f"Bearer {reader.token}x"}
            )
            assert invalid.status_code == 401
            expired = await client.get(
                "/v1/processes",
                headers={"Authorization": f"Bearer {expiring.token}"},
            )
            assert expired.status_code == 401
            assert expired.json()["detail"] == "bearer credential has expired"

            reader_headers = {
                "Authorization": f"Bearer {reader.token}",
                # Bearer identity is authoritative; an unsafe tenant header is ignored.
                "X-Tiramisu-Tenant-ID": str(other_tenant_id),
            }
            processes = await client.get("/v1/processes", headers=reader_headers)
            assert processes.status_code == 200
            assert processes.json() == []

            insufficient_scope = await client.get("/v1/reviews", headers=reader_headers)
            assert insufficient_scope.status_code == 403
            assert "reviews:read" in insufficient_scope.json()["detail"]

            reader_cannot_ingest = await client.post(
                "/v1/events",
                headers=reader_headers,
                json={
                    "event_type": "customer.email_received",
                    "source": "stub.email",
                    "source_event_id": f"email-{uuid4()}",
                    "occurred_at": datetime.now(UTC).isoformat(),
                },
            )
            assert reader_cannot_ingest.status_code == 403
            ingress_headers = {"Authorization": f"Bearer {ingress.token}"}
            ingested = await client.post(
                "/v1/events",
                headers=ingress_headers,
                json={
                    "event_type": "customer.email_received",
                    "source": "stub.email",
                    "source_event_id": f"email-{uuid4()}",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "external_references": [
                        {
                            "provider": "stub.email",
                            "resource_type": "conversation",
                            "external_id": f"conversation-{uuid4()}",
                        }
                    ],
                },
            )
            assert ingested.status_code == 202
            assert ingested.json()["correlation_status"] == "pending"
            assert (await client.get("/v1/processes", headers=ingress_headers)).status_code == 403

            async with admin_factory.begin() as session:
                transition = await safety_service.set_status(
                    session,
                    tenant_id=tenant_id,
                    actor_id=control_actor_id,
                    new_status="suspended",
                    reason="Unexpected provider behaviour under investigation",
                    metadata={"incident": "INC-42"},
                )
            assert transition.previous_status == "active"
            assert transition.new_status == "suspended"

            suspended = await client.get("/v1/processes", headers=reader_headers)
            assert suspended.status_code == 403
            assert suspended.json()["detail"] == "tenant is suspended"

            async with admin_factory.begin() as session:
                with pytest.raises(SafetyControlConflict, match="already suspended"):
                    await safety_service.set_status(
                        session,
                        tenant_id=tenant_id,
                        actor_id=control_actor_id,
                        new_status="suspended",
                        reason="Duplicate command",
                    )
                resumed = await safety_service.set_status(
                    session,
                    tenant_id=tenant_id,
                    actor_id=control_actor_id,
                    new_status="active",
                    reason="Incident resolved and provider disabled",
                )
            assert resumed.previous_status == "suspended"

            restored = await client.get("/v1/processes", headers=reader_headers)
            assert restored.status_code == 200

            async with admin_factory.begin() as session:
                revoked = await credential_service.revoke(
                    session,
                    tenant_id=tenant_id,
                    credential_id=reader.credential_id,
                    actor_id=control_actor_id,
                )
                repeated = await credential_service.revoke(
                    session,
                    tenant_id=tenant_id,
                    credential_id=reader.credential_id,
                    actor_id=control_actor_id,
                )
            assert repeated == revoked

            inactive = await client.get("/v1/processes", headers=reader_headers)
            assert inactive.status_code == 401

        async with admin_factory.begin() as session:
            events = (
                await session.scalars(
                    select(TenantSafetyEvent)
                    .where(TenantSafetyEvent.tenant_id == tenant_id)
                    .order_by(TenantSafetyEvent.created_at)
                )
            ).all()
            stored_credential = await session.get(TenantCredential, reader.credential_id)
        assert [(event.previous_status, event.new_status) for event in events] == [
            ("active", "suspended"),
            ("suspended", "active"),
        ]
        assert events[0].reason == "Unexpected provider behaviour under investigation"
        assert events[0].metadata_ == {"incident": "INC-42"}
        assert stored_credential is not None
        assert reader.token not in stored_credential.secret_hash
        assert stored_credential.status == "revoked"
    finally:
        await _delete_tenant_data(admin_factory, tenant_id)
        await runtime_engine.dispose()
        await admin_engine.dispose()
