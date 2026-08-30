"""Explicit action adapter registration performed before worker polling."""

from collections.abc import Mapping

from tiramisu_agents.core.ports.actions import ActionAdapter


class ActionAdapterRegistry:
    def __init__(self, bindings: Mapping[str, ActionAdapter]) -> None:
        self._bindings = dict(bindings)

    def resolve(self, action_type: str) -> ActionAdapter:
        try:
            return self._bindings[action_type]
        except KeyError as error:
            raise LookupError(f"no action adapter registered for: {action_type}") from error
