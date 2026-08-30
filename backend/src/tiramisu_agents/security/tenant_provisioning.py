"""Trusted tenant provisioning, including a repeatable local-development tenant."""

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.db.models.tenancy import Tenant
from tiramisu_agents.db.session import set_tenant_context

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

LOCAL_DEVELOPMENT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
LOCAL_DEVELOPMENT_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
LOCAL_DEVELOPMENT_TENANT_SLUG = "local-fictional"
LOCAL_DEVELOPMENT_TENANT_NAME = "Local Fictional Tenant"


class TenantProvisioningConflict(ValueError):
    """Raised when a tenant ID or slug is already owned by another tenant."""


@dataclass(frozen=True, slots=True)
class ProvisionedTenant:
    tenant_id: UUID
    slug: str
    name: str
    created: bool


class TenantProvisioningService:
    """Control-plane tenant creation; application runtime roles cannot create tenants."""

    async def create(
        self,
        session: AsyncSession,
        *,
        slug: str,
        name: str,
        tenant_id: UUID | None = None,
    ) -> ProvisionedTenant:
        normalized_slug, normalized_name = _normalize_tenant_values(slug=slug, name=name)
        resolved_tenant_id = tenant_id or uuid4()
        await set_tenant_context(session, resolved_tenant_id)
        existing = await _find_existing(session, tenant_id=resolved_tenant_id, slug=normalized_slug)
        if existing is not None:
            raise TenantProvisioningConflict(
                f"tenant already exists with ID {existing.id} or slug {existing.slug!r}"
            )

        tenant = Tenant(id=resolved_tenant_id, slug=normalized_slug, name=normalized_name)
        session.add(tenant)
        try:
            await session.flush()
        except IntegrityError as error:
            # A tenant-scoped RLS context deliberately cannot reveal another
            # tenant's slug. Let PostgreSQL enforce that global uniqueness, but
            # present the trusted CLI with the same safe, domain-level error.
            raise TenantProvisioningConflict(
                "tenant already exists with this ID or slug"
            ) from error
        return ProvisionedTenant(
            tenant_id=tenant.id,
            slug=tenant.slug,
            name=tenant.name,
            created=True,
        )

    async def ensure_local_development_tenant(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID = LOCAL_DEVELOPMENT_TENANT_ID,
    ) -> ProvisionedTenant:
        """Create the documented fictional tenant once, without overwriting any tenant."""
        await set_tenant_context(session, tenant_id)
        existing = await _find_existing(
            session,
            tenant_id=tenant_id,
            slug=LOCAL_DEVELOPMENT_TENANT_SLUG,
        )
        if existing is not None:
            if (
                existing.id != tenant_id
                or existing.slug != LOCAL_DEVELOPMENT_TENANT_SLUG
                or existing.name != LOCAL_DEVELOPMENT_TENANT_NAME
            ):
                raise TenantProvisioningConflict(
                    "the local-development tenant ID or slug is already assigned to a different "
                    "tenant; choose a separate database or resolve the conflict explicitly"
                )
            return ProvisionedTenant(
                tenant_id=existing.id,
                slug=existing.slug,
                name=existing.name,
                created=False,
            )
        return await self.create(
            session,
            tenant_id=tenant_id,
            slug=LOCAL_DEVELOPMENT_TENANT_SLUG,
            name=LOCAL_DEVELOPMENT_TENANT_NAME,
        )


def _normalize_tenant_values(*, slug: str, name: str) -> tuple[str, str]:
    normalized_slug = slug.strip().lower()
    normalized_name = name.strip()
    if _SLUG_PATTERN.fullmatch(normalized_slug) is None:
        raise ValueError(
            "tenant slug must contain 1 to 63 lowercase letters, digits, or hyphens, "
            "starting with a letter"
        )
    if not normalized_name or len(normalized_name) > 255:
        raise ValueError("tenant name must contain 1 to 255 characters")
    return normalized_slug, normalized_name


async def _find_existing(
    session: AsyncSession, *, tenant_id: UUID | None, slug: str
) -> Tenant | None:
    conditions = [Tenant.slug == slug]
    if tenant_id is not None:
        conditions.append(Tenant.id == tenant_id)
    return await session.scalar(select(Tenant).where(or_(*conditions)))
