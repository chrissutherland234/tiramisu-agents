"""One fail-fast composition shared by fictional API ingress and worker execution."""

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from tiramisu_agents import __version__
from tiramisu_agents.adapters.stubs import StubBusinessState, stub_business_bindings
from tiramisu_agents.core.ports.actions import ActionAdapter
from tiramisu_agents.events.ingestion import ProcessBootstrap
from tiramisu_agents.extensions import ExtensionManifest
from tiramisu_agents.processes.definitions import ProcessDefinition
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry


class FictionalDeploymentError(ValueError):
    """Raised when bundled extension metadata and runtime bindings disagree."""


@dataclass(frozen=True, slots=True)
class FictionalDeployment:
    manifest: ExtensionManifest
    registry: ProcessDefinitionRegistry
    definition: ProcessDefinition
    bindings: dict[str, ActionAdapter]

    def trigger_rules(self) -> dict[str, ProcessBootstrap]:
        bootstrap = ProcessBootstrap(
            process_type=self.definition.id,
            definition_version=self.definition.version,
            extension_manifest_hash=self.manifest.fingerprint(),
        )
        return dict.fromkeys(self.definition.trigger_events, bootstrap)


def load_fictional_deployment(*, state: StubBusinessState | None = None) -> FictionalDeployment:
    resources = files("tiramisu_agents.builtin")
    manifest_document: Any = json.loads(
        resources.joinpath("extension_manifest.json").read_text(encoding="utf-8")
    )
    definition_document: Any = yaml.safe_load(
        resources.joinpath("enquiry_to_booking.v1.yaml").read_text(encoding="utf-8")
    )
    manifest = ExtensionManifest.model_validate(manifest_document)
    definition = ProcessDefinition.model_validate(definition_document)
    registry = ProcessDefinitionRegistry([definition])
    bindings = stub_business_bindings(state or StubBusinessState())

    try:
        compatible = Version(__version__) in SpecifierSet(manifest.tiramisu_compatibility)
    except InvalidSpecifier as error:
        raise FictionalDeploymentError(
            "fictional pack has invalid compatibility metadata"
        ) from error
    if not compatible:
        raise FictionalDeploymentError(f"fictional pack does not support Tiramisu {__version__}")
    expected_definition = f"{definition.id}.v{definition.version}"
    if manifest.process_definitions != (expected_definition,):
        raise FictionalDeploymentError("fictional manifest and process identity disagree")
    if set(manifest.adapters) != set(definition.integrations.values()):
        raise FictionalDeploymentError("fictional manifest and integration bindings disagree")
    if {adapter.id for adapter in bindings.values()} != set(manifest.adapters):
        raise FictionalDeploymentError("fictional adapter registry is incomplete")
    if set(bindings) != set(definition.allowed_actions):
        raise FictionalDeploymentError("fictional action policy and adapter registry disagree")
    if manifest.policies != ("fictional.default.v1",):
        raise FictionalDeploymentError("fictional policy registration is incomplete")

    return FictionalDeployment(
        manifest=manifest,
        registry=registry,
        definition=definition,
        bindings=bindings,
    )
