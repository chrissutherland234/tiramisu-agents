"""Application configuration for one client-pack deployment release."""

from uuid import UUID

from tiramisu_agents.api.settings import Settings
from tiramisu_agents.extensions import ClientPack, DeploymentRelease


def compose_deployment_release(
    settings: Settings,
    client_pack: ClientPack,
    *,
    tenant_ids: tuple[UUID, ...] | None = None,
) -> DeploymentRelease:
    if settings.deployment_id is None:
        raise ValueError("TIRAMISU_DEPLOYMENT_ID is required for a client-pack deployment")
    if settings.deployment_build_id is None:
        raise ValueError("TIRAMISU_DEPLOYMENT_BUILD_ID is required for a client-pack deployment")
    assigned_tenant_ids = settings.deployment_tenant_ids if tenant_ids is None else tenant_ids
    if not assigned_tenant_ids:
        raise ValueError(
            "TIRAMISU_DEPLOYMENT_TENANT_IDS must assign at least one tenant to the deployment"
        )
    if settings.openai_model is None:
        raise ValueError("TIRAMISU_OPENAI_MODEL is required for a client-pack deployment")
    return DeploymentRelease(
        deployment_id=settings.deployment_id,
        build_id=settings.deployment_build_id,
        client_pack_fingerprint=client_pack.fingerprint(),
        model_id=settings.openai_model,
    )
