"""Explicit, versioned deployment extension contracts."""

from tiramisu_agents.extensions.deployment import ClientPack, ClientPackError
from tiramisu_agents.extensions.loader import load_client_pack, load_configured_client_pack
from tiramisu_agents.extensions.manifest import ExtensionManifest
from tiramisu_agents.extensions.project_metadata import ProjectDescription
from tiramisu_agents.extensions.runtime import DeploymentRelease, normalize_deployment_id
from tiramisu_agents.processes.compatibility import (
    DeploymentCompatibility,
    DeploymentCompatibilityError,
)

__all__ = [
    "ClientPack",
    "ClientPackError",
    "DeploymentCompatibility",
    "DeploymentCompatibilityError",
    "DeploymentRelease",
    "ExtensionManifest",
    "ProjectDescription",
    "load_client_pack",
    "load_configured_client_pack",
    "normalize_deployment_id",
]
