"""Explicit import-path loading for deployment-controlled client packs."""

from importlib import import_module
from typing import Any

from tiramisu_agents.extensions.deployment import ClientPack, ClientPackError

FICTIONAL_CLIENT_PACK_FACTORY = "tiramisu_agents.builtin:load_fictional_deployment"


def configured_client_pack_factory(
    factory_path: str | None, *, load_fictional_example: bool
) -> str | None:
    if factory_path and load_fictional_example:
        raise ClientPackError(
            "configure either a client-pack factory or the fictional example, not both"
        )
    return FICTIONAL_CLIENT_PACK_FACTORY if load_fictional_example else factory_path


def load_client_pack(factory_path: str) -> ClientPack:
    """Load one explicitly trusted, zero-argument factory at process startup."""
    module_name, separator, attribute_name = factory_path.partition(":")
    if not separator or not module_name.strip() or not attribute_name.strip():
        raise ClientPackError("client-pack factory must use the form 'module:attribute'")
    if ":" in attribute_name:
        raise ClientPackError("client-pack factory must contain exactly one ':' separator")
    try:
        module = import_module(module_name)
    except ImportError as error:
        raise ClientPackError(f"could not import client-pack module {module_name!r}") from error
    try:
        factory: Any = getattr(module, attribute_name)
    except AttributeError as error:
        raise ClientPackError(
            f"client-pack module {module_name!r} has no attribute {attribute_name!r}"
        ) from error
    if not callable(factory):
        raise ClientPackError("configured client-pack factory is not callable")
    deployment: Any = factory()
    if not isinstance(deployment, ClientPack):
        raise ClientPackError("configured factory did not return a ClientPack")
    return deployment


def load_configured_client_pack(
    factory_path: str | None, *, load_fictional_example: bool
) -> ClientPack | None:
    resolved = configured_client_pack_factory(
        factory_path, load_fictional_example=load_fictional_example
    )
    return load_client_pack(resolved) if resolved is not None else None
