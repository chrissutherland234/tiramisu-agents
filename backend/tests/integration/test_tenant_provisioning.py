"""PostgreSQL coverage for trusted tenant provisioning."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete
from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import create_engine, create_session_factory
from tiramisu_agents.security.tenant_provisioning import (
    LOCAL_DEVELOPMENT_TENANT_ID,
    TenantProvisioningConflict,
    TenantProvisioningService,
)

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


@pytest.mark.asyncio
async def test_tenant_creation_is_unique_and_local_bootstrap_is_idempotent() -> None:
    migration_url = os.getenv(
        "TIRAMISU_MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
    )
    engine = create_engine(migration_url)
    session_factory = create_session_factory(engine)
    service = TenantProvisioningService()
    tenant_id = uuid4()

    try:
        async with session_factory.begin() as session:
            created = await service.create(
                session,
                tenant_id=tenant_id,
                slug=f"test-{tenant_id}",
                name="Test tenant",
            )
            first_bootstrap = await service.ensure_local_development_tenant(session)

        assert created.tenant_id == tenant_id
        assert created.created is True
        assert first_bootstrap.tenant_id == LOCAL_DEVELOPMENT_TENANT_ID

        async with session_factory.begin() as session:
            repeated_bootstrap = await service.ensure_local_development_tenant(session)
            with pytest.raises(TenantProvisioningConflict, match="already exists"):
                await service.create(
                    session,
                    slug=f"test-{tenant_id}",
                    name="Duplicate tenant",
                )

        assert repeated_bootstrap.created is False
    finally:
        async with session_factory.begin() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await engine.dispose()
