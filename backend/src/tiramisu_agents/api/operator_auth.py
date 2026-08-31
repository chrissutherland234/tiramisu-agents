"""Tenant-bound bearer authentication with an explicit local-development fallback."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tiramisu_agents.api.settings import Settings
from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential
from tiramisu_agents.db.session import set_tenant_context
from tiramisu_agents.security.credentials import (
    CredentialScope,
    credential_secret_matches,
    parse_credential,
)
from tiramisu_agents.security.tenancy import (
    TenantNotAuthorized,
    TenantUnavailable,
    require_tenant_deployment,
)

_DUMMY_SECRET_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    tenant_id: UUID
    actor_id: UUID
    scopes: frozenset[str]
    roles: frozenset[str]
    credential_id: UUID | None
    authentication_method: str

    def require_scope(self, scope: CredentialScope) -> None:
        if "*" not in self.scopes and scope.value not in self.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"credential lacks required scope: {scope.value}",
            )

    def has_role(self, role: str) -> bool:
        return "*" in self.roles or role in self.roles


async def require_operator_identity(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    tenant_header: Annotated[str | None, Header(alias="X-Tiramisu-Tenant-ID")] = None,
    actor_header: Annotated[str | None, Header(alias="X-Tiramisu-Actor-ID")] = None,
) -> OperatorIdentity:
    if authorization is not None:
        identity = await _authenticate_bearer(request, authorization)
        return await _require_deployment_assignment(request, identity)

    settings: Settings = request.app.state.settings
    supplied_unsafe_header = tenant_header is not None or actor_header is not None
    if settings.environment == "development" and settings.allow_unsafe_development_tenant_header:
        if tenant_header is None or actor_header is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Tiramisu-Tenant-ID and X-Tiramisu-Actor-ID are required",
            )
        try:
            tenant_id = UUID(tenant_header)
            actor_id = UUID(actor_header)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="operator identity headers must be UUIDs",
            ) from error
        identity = OperatorIdentity(
            tenant_id=tenant_id,
            actor_id=actor_id,
            scopes=frozenset({"*"}),
            roles=frozenset({"*"}),
            credential_id=None,
            authentication_method="unsafe_development_headers",
        )
        return await _require_deployment_assignment(request, identity)
    if supplied_unsafe_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="development identity headers are disabled",
        )
    raise _unauthorized("bearer credential is required")


async def require_event_ingress_identity(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    tenant_header: Annotated[str | None, Header(alias="X-Tiramisu-Tenant-ID")] = None,
) -> OperatorIdentity:
    if authorization is not None:
        identity = await _authenticate_bearer(request, authorization)
    else:
        settings: Settings = request.app.state.settings
        unsafe_headers_enabled = (
            settings.environment == "development"
            and settings.allow_unsafe_development_tenant_header
        )
        if not unsafe_headers_enabled:
            if tenant_header is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="development identity headers are disabled",
                )
            raise _unauthorized("bearer credential is required")
        if tenant_header is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Tiramisu-Tenant-ID is required",
            )
        try:
            tenant_id = UUID(tenant_header)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Tiramisu-Tenant-ID must be a UUID",
            ) from error
        identity = OperatorIdentity(
            tenant_id=tenant_id,
            actor_id=UUID(int=0),
            scopes=frozenset({"*"}),
            roles=frozenset(),
            credential_id=None,
            authentication_method="unsafe_development_headers",
        )
    identity.require_scope(CredentialScope.EVENTS_INGEST)
    return await _require_deployment_assignment(request, identity)


async def require_process_reader(
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> OperatorIdentity:
    identity.require_scope(CredentialScope.PROCESSES_READ)
    return identity


async def require_review_reader(
    identity: Annotated[OperatorIdentity, Depends(require_operator_identity)],
) -> OperatorIdentity:
    identity.require_scope(CredentialScope.REVIEWS_READ)
    return identity


async def _authenticate_bearer(request: Request, authorization: str) -> OperatorIdentity:
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise _unauthorized("bearer credential is malformed")
    try:
        parsed = parse_credential(token)
    except ValueError as error:
        raise _unauthorized("bearer credential is invalid") from error

    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory.begin() as session:
        await set_tenant_context(session, parsed.tenant_id)
        row = (
            await session.execute(
                select(TenantCredential, Tenant.status)
                .join(Tenant, Tenant.id == TenantCredential.tenant_id)
                .where(
                    TenantCredential.id == parsed.credential_id,
                    TenantCredential.tenant_id == parsed.tenant_id,
                )
            )
        ).one_or_none()
        expected_hash = row[0].secret_hash if row is not None else _DUMMY_SECRET_HASH
        secret_valid = credential_secret_matches(parsed.secret, expected_hash)
        if row is None or not secret_valid:
            raise _unauthorized("bearer credential is invalid")
        credential, tenant_status = row
        if credential.status != "active":
            raise _unauthorized("bearer credential is inactive")
        if credential.expires_at is not None and credential.expires_at <= datetime.now(UTC):
            raise _unauthorized("bearer credential has expired")
        if tenant_status != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="tenant is suspended",
            )
        return OperatorIdentity(
            tenant_id=credential.tenant_id,
            actor_id=credential.actor_id,
            scopes=frozenset(str(scope) for scope in credential.scopes),
            roles=frozenset(str(role) for role in credential.roles),
            credential_id=credential.id,
            authentication_method="tenant_bearer",
        )


async def _require_deployment_assignment(
    request: Request,
    identity: OperatorIdentity,
) -> OperatorIdentity:
    release = getattr(request.app.state, "deployment_release", None)
    if release is None:
        return identity
    tenant_ids: frozenset[UUID] = request.app.state.deployment_tenant_ids
    if identity.tenant_id not in tenant_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant is not in this deployment's allow-list",
        )
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    try:
        async with session_factory.begin() as session:
            await require_tenant_deployment(
                session,
                identity.tenant_id,
                release.deployment_id,
            )
    except (TenantUnavailable, TenantNotAuthorized) as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant is not assigned to this deployment",
        ) from error
    return identity


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
