"""Explicit, versioned deployment extension contracts."""

from tiramisu_agents.extensions.deployment import ClientPack, ClientPackError
from tiramisu_agents.extensions.loader import load_client_pack, load_configured_client_pack
from tiramisu_agents.extensions.manifest import ExtensionManifest
from tiramisu_agents.processes.compatibility import (
    DeploymentCompatibility,
    DeploymentCompatibilityError,
)

__all__ = [
    "ClientPack",
    "ClientPackError",
    "DeploymentCompatibility",
    "DeploymentCompatibilityError",
    "ExtensionManifest",
    "load_client_pack",
    "load_configured_client_pack",
]
