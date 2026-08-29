"""PostgreSQL integration checks. Enable with TIRAMISU_RUN_DB_TESTS=1."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from tiramisu_agents.db.session import create_engine, create_session_factory, set_tenant_context

pytestmark = pytest.mark.skipif(
    os.getenv("TIRAMISU_RUN_DB_TESTS") != "1",
    reason="requires the migrated PostgreSQL integration database",
)


@pytest.mark.asyncio
async def test_rls_exposes_only_the_selected_tenant() -> None:
    runtime_database_url = os.getenv(
        "TIRAMISU_DATABASE_URL",
        "postgresql+asyncpg://tiramisu_app:tiramisu_app@localhost:5432/tiramisu_test",
    )
    migration_database_url = os.getenv(
        "TIRAMISU_MIGRATION_DATABASE_URL",
        "postgresql+asyncpg://tiramisu:tiramisu@localhost:5432/tiramisu_test",
    )
    tenant_a = uuid4()
    tenant_b = uuid4()
    admin_engine = create_engine(migration_database_url)
    runtime_engine = create_engine(runtime_database_url)

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (:tenant_a, :slug_a, 'Tenant A'), "
                    "(:tenant_b, :slug_b, 'Tenant B')"
                ),
                {
                    "tenant_a": tenant_a,
                    "tenant_b": tenant_b,
                    "slug_a": f"tenant-{tenant_a}",
                    "slug_b": f"tenant-{tenant_b}",
                },
            )
            tenant_privileges = (
                await connection.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE grantee = 'tiramisu_app' AND table_name = 'tenants'"
                    )
                )
            ).scalars()
            assert set(tenant_privileges) == {"SELECT"}

        session_factory = create_session_factory(runtime_engine)
        async with session_factory.begin() as session:
            await set_tenant_context(session, tenant_a)
            visible_ids = (await session.execute(text("SELECT id FROM tenants"))).scalars().all()
            assert visible_ids == [tenant_a]
    finally:
        async with admin_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id IN (:tenant_a, :tenant_b)"),
                {"tenant_a": tenant_a, "tenant_b": tenant_b},
            )
        await runtime_engine.dispose()
        await admin_engine.dispose()
