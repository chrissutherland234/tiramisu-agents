"""Trusted control-plane operations for bearer credential lifecycle."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.security.credentials import CredentialScope, IssuedCredential, issue_credential
from tiramisu_agents.security.tenancy import TenantUnavailable

_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")


@dataclass(frozen=True, slots=True)
class RevokedCredential:
    credential_id: UUID
    revoked_at: datetime


class TenantCredentialService:
    async def issue(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        name: str,
        scopes: tuple[CredentialScope, ...],
        roles: tuple[str, ...] = (),
        expires_at: datetime | None = None,
    ) -> IssuedCredential:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 255:
            raise ValueError("credential name must contain 1 to 255 characters")
        if not scopes:
            raise ValueError("credential requires at least one scope")
        if len(scopes) != len(set(scopes)):
            raise ValueError("credential scopes must be unique")
        normalized_roles = tuple(role.strip() for role in roles)
        if len(normalized_roles) != len(set(normalized_roles)):
            raise ValueError("credential roles must be unique")
        if any(_ROLE_PATTERN.fullmatch(role) is None for role in normalized_roles):
            raise ValueError("credential role has an invalid identifier")
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("credential expiry must be timezone-aware")
            if expires_at <= datetime.now(UTC):
                raise ValueError("credential expiry must be in the future")

        await set_tenant_context(session, tenant_id)
        if await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id)) is None:
            raise TenantUnavailable(str(tenant_id))
        issued = issue_credential(tenant_id, actor_id)
        session.add(
            TenantCredential(
                id=issued.credential_id,
                tenant_id=tenant_id,
                actor_id=actor_id,
                name=normalized_name,
                secret_hash=issued.secret_hash,
                scopes=[scope.value for scope in scopes],
                roles=list(normalized_roles),
                expires_at=expires_at,
            )
        )
        await session.flush()
        return issued

    async def revoke(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        credential_id: UUID,
        actor_id: UUID,
    ) -> RevokedCredential:
        await set_tenant_context(session, tenant_id)
        credential = await session.scalar(
            select(TenantCredential).where(TenantCredential.id == credential_id).with_for_update()
        )
        if credential is None:
            raise TenantUnavailable(str(credential_id))
        if credential.status == "revoked":
            if credential.revoked_at is None:
                raise RuntimeError("revoked credential is missing its audit timestamp")
            return RevokedCredential(
                credential_id=credential.id,
                revoked_at=credential.revoked_at,
            )
        revoked_at = datetime.now(UTC)
        credential.status = "revoked"
        credential.revoked_at = revoked_at
        credential.revoked_by_actor_id = actor_id
        await session.flush()
        return RevokedCredential(credential_id=credential.id, revoked_at=revoked_at)
