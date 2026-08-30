"""Explicitly unsafe local operator identity until production authentication exists."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Header, HTTPException, Request, status

from tiramisu_agents.api.settings import Settings


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    tenant_id: UUID
    actor_id: UUID


async def require_operator_identity(
    request: Request,
    tenant_header: Annotated[str | None, Header(alias="X-Tiramisu-Tenant-ID")] = None,
    actor_header: Annotated[str | None, Header(alias="X-Tiramisu-Actor-ID")] = None,
) -> OperatorIdentity:
    settings: Settings = request.app.state.settings
    if settings.environment != "development" or not settings.allow_unsafe_development_tenant_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="development operator identity headers are disabled",
        )
    if tenant_header is None or actor_header is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tiramisu-Tenant-ID and X-Tiramisu-Actor-ID are required",
        )
    try:
        return OperatorIdentity(tenant_id=UUID(tenant_header), actor_id=UUID(actor_header))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="operator identity headers must be UUIDs",
        ) from error
