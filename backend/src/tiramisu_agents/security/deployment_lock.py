"""Transaction locks that serialize tenant assignment against process creation."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_tenant_deployment_for_ingress(session: AsyncSession, tenant_id: UUID) -> None:
    """Allow concurrent ingress while blocking an assignment change."""

    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {"key": f"tenant-deployment:{tenant_id}"},
    )


async def lock_tenant_deployment_for_assignment(session: AsyncSession, tenant_id: UUID) -> None:
    """Wait for ingress to finish and exclude new process creation until commit."""

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"tenant-deployment:{tenant_id}"},
    )
