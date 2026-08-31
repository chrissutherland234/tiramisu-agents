"""Stable startup-time contract for a separately installed client pack."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from pydantic import BaseModel

from tiramisu_agents import __version__
from tiramisu_agents.core.ports.actions import ActionAdapter
from tiramisu_agents.events.ingestion import ProcessBootstrap
from tiramisu_agents.extensions.manifest import ExtensionManifest
from tiramisu_agents.processes.definitions import ProcessDefinition
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry


class ClientPackError(ValueError):
    """Raised when a client pack cannot safely be composed at startup."""


@dataclass(frozen=True, slots=True)
class ClientPack:
    """A complete, immutable API/worker deployment unit supplied by one package.

    Factories construct this object before a Temporal worker begins polling. The
    kernel validates all declarative identities against the concrete runtime
    bindings, so an API and worker configured with the same factory cannot drift.
    """

    manifest: ExtensionManifest
    definitions: tuple[ProcessDefinition, ...]
    bindings: Mapping[str, ActionAdapter]
    agent_decision_output_type: type[BaseModel]
    policy_ids: tuple[str, ...]
    registry: ProcessDefinitionRegistry = field(init=False, repr=False)

    def __post_init__(self) -> None:
        definitions = tuple(self.definitions)
        bindings = MappingProxyType(dict(self.bindings))
        policy_ids = tuple(self.policy_ids)
        object.__setattr__(self, "definitions", definitions)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "policy_ids", policy_ids)

        if not definitions:
            raise ClientPackError("a client pack must register at least one process definition")
        try:
            compatible = Version(__version__) in SpecifierSet(self.manifest.tiramisu_compatibility)
        except InvalidSpecifier as error:
            raise ClientPackError(
                "client pack has invalid Tiramisu compatibility metadata"
            ) from error
        if not compatible:
            raise ClientPackError(
                f"client pack {self.manifest.extension_id} does not support Tiramisu {__version__}"
            )
        if self.manifest.activities:
            raise ClientPackError(
                "custom Temporal activities are not supported by the client-pack contract yet"
            )

        expected_definitions = {f"{item.id}.v{item.version}" for item in definitions}
        if set(self.manifest.process_definitions) != expected_definitions:
            raise ClientPackError("manifest and process definition identities disagree")
        expected_adapters = {
            adapter_id
            for definition in definitions
            for adapter_id in definition.integrations.values()
        }
        if set(self.manifest.adapters) != expected_adapters:
            raise ClientPackError("manifest and process integration identities disagree")
        if {adapter.id for adapter in bindings.values()} != set(self.manifest.adapters):
            raise ClientPackError("adapter bindings do not cover the manifest")
        expected_actions = {
            action_type for definition in definitions for action_type in definition.allowed_actions
        }
        if set(bindings) != expected_actions:
            raise ClientPackError("action bindings do not cover the process definitions")
        if set(policy_ids) != set(self.manifest.policies):
            raise ClientPackError("policy registrations do not cover the manifest")
        if not callable(getattr(self.agent_decision_output_type, "to_agent_decision", None)):
            raise ClientPackError("agent decision output type must implement to_agent_decision")
        registry = ProcessDefinitionRegistry(definitions)
        # Resolve every trigger now so ambiguous configuration fails before API
        # traffic or worker polling begins.
        for event_type in self.trigger_rules():
            registry.resolve_trigger(event_type, include_drafts=True)
        object.__setattr__(self, "registry", registry)

    @property
    def definition(self) -> ProcessDefinition:
        """Convenience accessor for the common single-process client pack."""
        if len(self.definitions) != 1:
            raise ClientPackError("client pack contains more than one process definition")
        return self.definitions[0]

    def trigger_rules(self) -> dict[str, ProcessBootstrap]:
        rules: dict[str, ProcessBootstrap] = {}
        for definition in self.definitions:
            bootstrap = ProcessBootstrap(
                process_type=definition.id,
                definition_version=definition.version,
                extension_manifest_hash=self.manifest.fingerprint(),
            )
            for event_type in definition.trigger_events:
                if event_type in rules:
                    raise ClientPackError(f"ambiguous client-pack trigger: {event_type}")
                rules[event_type] = bootstrap
        return rules
