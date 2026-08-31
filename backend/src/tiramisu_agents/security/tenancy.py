"""Live tenant status checks and auditable control-plane transitions."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.db.models.tenancy import Tenant, TenantSafetyEvent
from tiramisu_agents.db.session import set_tenant_context


class TenantUnavailable(LookupError):
    """Raised when a tenant is missing or invisible in the current authority boundary."""


class TenantSuspended(PermissionError):
    """Raised when a live control forbids autonomous work for a tenant."""


class TenantNotAuthorized(PermissionError):
    """Raised when a worker deployment is not assigned to a tenant."""


class SafetyControlConflict(ValueError):
    """Raised when a requested safety transition does not change current state."""


async def require_tenant_deployment(
    session: AsyncSession,
    tenant_id: UUID,
    deployment_id: str,
) -> None:
    await set_tenant_context(session, tenant_id)
    assigned_deployment = await session.scalar(
        select(Tenant.deployment_id).where(Tenant.id == tenant_id)
    )
    if assigned_deployment is None:
        raise TenantUnavailable(str(tenant_id))
    if assigned_deployment != deployment_id:
        raise TenantNotAuthorized(str(tenant_id))


async def require_active_tenant(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    deployment_id: str | None = None,
) -> None:
    await set_tenant_context(session, tenant_id)
    row = (
        await session.execute(
            select(Tenant.status, Tenant.deployment_id).where(Tenant.id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise TenantUnavailable(str(tenant_id))
    tenant_status, assigned_deployment = row
    if deployment_id is not None and assigned_deployment != deployment_id:
        raise TenantNotAuthorized(str(tenant_id))
    if tenant_status != "active":
        raise TenantSuspended(str(tenant_id))


def require_authorized_tenant(
    tenant_id: UUID,
    authorized_tenant_ids: frozenset[UUID] | None,
) -> None:
    if authorized_tenant_ids is not None and tenant_id not in authorized_tenant_ids:
        raise TenantNotAuthorized(str(tenant_id))


@dataclass(frozen=True, slots=True)
class SafetyTransition:
    event_id: UUID
    previous_status: str
    new_status: str


class TenantSafetyService:
    async def set_status(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        new_status: Literal["active", "suspended"],
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> SafetyTransition:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("a safety transition requires a reason")
        if len(normalized_reason) > 10_000:
            raise ValueError("safety transition reason is too long")
        await set_tenant_context(session, tenant_id)
        tenant = await session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise TenantUnavailable(str(tenant_id))
        previous_status = tenant.status
        if previous_status == new_status:
            raise SafetyControlConflict(f"tenant is already {new_status}")
        event = TenantSafetyEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            previous_status=previous_status,
            new_status=new_status,
            reason=normalized_reason,
            metadata_=metadata or {},
        )
        tenant.status = new_status
        session.add(event)
        await session.flush()
        return SafetyTransition(
            event_id=event.id,
            previous_status=previous_status,
            new_status=new_status,
        )
