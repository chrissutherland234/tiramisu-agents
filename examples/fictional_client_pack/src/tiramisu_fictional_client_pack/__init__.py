"""Minimal external-package factory used to demonstrate editable composition.

The sample delegates its business configuration to Tiramisu's public fictional
pack so the repository has only one canonical demo definition. A real private
package would construct and return ``ClientPack`` from its own packaged
definitions, adapters, policies, and strict agent output model.
"""

from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.extensions import ClientPack


def create_client_pack() -> ClientPack:
    return load_fictional_deployment()


__all__ = ["create_client_pack"]
