"""Audited control-plane changes to a tenant's logical deployment."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.db.models.events import OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant, TenantDeploymentEvent
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.extensions.runtime import normalize_deployment_id
from tiramisu_agents.security.deployment_lock import lock_tenant_deployment_for_assignment
from tiramisu_agents.security.tenancy import TenantUnavailable


class TenantDeploymentConflict(ValueError):
    """Raised when a tenant cannot safely move between logical deployments."""


@dataclass(frozen=True, slots=True)
class TenantDeploymentResult:
    tenant_id: UUID
    previous_deployment_id: str
    deployment_id: str
    changed: bool
    event_id: UUID | None


class TenantDeploymentService:
    async def assign(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        deployment_id: str,
        actor_id: UUID,
        reason: str,
    ) -> TenantDeploymentResult:
        target = normalize_deployment_id(deployment_id)
        if target == "unassigned":
            raise ValueError("unassigned is reserved for legacy or unconfigured tenants")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("a tenant deployment assignment requires a reason")
        if len(normalized_reason) > 10_000:
            raise ValueError("tenant deployment assignment reason is too long")

        await lock_tenant_deployment_for_assignment(session, tenant_id)
        await set_tenant_context(session, tenant_id)
        tenant = await session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise TenantUnavailable(str(tenant_id))
        previous = tenant.deployment_id
        if previous == target:
            return TenantDeploymentResult(
                tenant_id=tenant_id,
                previous_deployment_id=previous,
                deployment_id=target,
                changed=False,
                event_id=None,
            )

        if previous != "unassigned":
            nonterminal_count = await session.scalar(
                select(func.count())
                .select_from(ProcessInstance)
                .where(
                    ProcessInstance.tenant_id == tenant_id,
                    ProcessInstance.status.not_in(("completed", "cancelled", "failed")),
                )
            )
            if nonterminal_count:
                raise TenantDeploymentConflict(
                    "tenant has active processes; move requires an audited active-instance "
                    "migration or terminal completion"
                )
            outstanding_delivery_count = await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .join(
                    ProcessInstance,
                    (ProcessInstance.tenant_id == OutboxMessage.tenant_id)
                    & (ProcessInstance.id == OutboxMessage.process_instance_id),
                )
                .where(
                    OutboxMessage.tenant_id == tenant_id,
                    ProcessInstance.deployment_id == previous,
                    OutboxMessage.status != "published",
                )
            )
            if outstanding_delivery_count:
                raise TenantDeploymentConflict(
                    "tenant has outstanding deliveries for its current deployment; "
                    "publish or explicitly resolve them before moving the tenant"
                )

        event = TenantDeploymentEvent(
            tenant_id=tenant_id,
            actor_id=actor_id,
            previous_deployment_id=previous,
            new_deployment_id=target,
            reason=normalized_reason,
        )
        tenant.deployment_id = target
        session.add(event)
        await session.flush()
        return TenantDeploymentResult(
            tenant_id=tenant_id,
            previous_deployment_id=previous,
            deployment_id=target,
            changed=True,
            event_id=event.id,
        )
