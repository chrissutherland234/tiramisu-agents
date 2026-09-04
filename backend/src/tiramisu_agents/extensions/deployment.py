"""Stable startup-time contract for a separately installed client pack."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version
from pydantic import BaseModel

from tiramisu_agents import __version__
from tiramisu_agents.core.ports.actions import ActionAdapter
from tiramisu_agents.events.ingestion import ProcessBootstrap
from tiramisu_agents.extensions.manifest import ExtensionManifest
from tiramisu_agents.extensions.project_metadata import ProjectDescription
from tiramisu_agents.extensions.runtime import DeploymentRelease
from tiramisu_agents.processes.compatibility import DeploymentCompatibility
from tiramisu_agents.processes.definitions import DefinitionStatus, ProcessDefinition
from tiramisu_agents.processes.registry import ProcessDefinitionRegistry


class ClientPackError(ValueError):
    """Raised when a client pack cannot safely be composed at startup."""


def _empty_action_bindings() -> dict[str, ActionAdapter]:
    return {}


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
    project: ProjectDescription | None = None
    simulation_bindings: Mapping[str, ActionAdapter] = field(default_factory=_empty_action_bindings)
    registry: ProcessDefinitionRegistry = field(init=False, repr=False)
    compatibility: DeploymentCompatibility = field(init=False, repr=False)
    _fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        definitions = tuple(self.definitions)
        bindings = MappingProxyType(dict(self.bindings))
        policy_ids = tuple(self.policy_ids)
        simulation_bindings = MappingProxyType(dict(self.simulation_bindings))
        object.__setattr__(self, "definitions", definitions)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "policy_ids", policy_ids)
        object.__setattr__(self, "simulation_bindings", simulation_bindings)

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
        if self.project is not None:
            if (
                self.project.id != self.manifest.extension_id
                or self.project.version != self.manifest.extension_version
            ):
                raise ClientPackError("project and manifest identities disagree")
            project_journeys = {
                (journey.id, journey.version): journey for journey in self.project.journeys
            }
            if set(project_journeys) != {
                (definition.id, definition.version) for definition in definitions
            }:
                raise ClientPackError("project metadata and process definitions disagree")
            for definition in definitions:
                journey = project_journeys[(definition.id, definition.version)]
                starts = {route.event_type for route in journey.routes if route.kind == "start"}
                wakes = {route.event_type for route in journey.routes if route.kind == "wake"}
                if starts != set(definition.trigger_events) or wakes != set(
                    definition.allowed_wake_events
                ):
                    raise ClientPackError("project route metadata and process definitions disagree")
                if journey.status != definition.status.value or journey.goals != definition.goals:
                    raise ClientPackError("project journey metadata and definitions disagree")
                if {item.action_type for item in journey.capabilities} != set(
                    definition.allowed_actions
                ):
                    raise ClientPackError(
                        "project capability metadata and process definitions disagree"
                    )
                if {
                    item.action_type: item.adapter_id for item in journey.capabilities
                } != definition.integrations:
                    raise ClientPackError(
                        "project adapter metadata and process definitions disagree"
                    )
                if journey.permissions != definition.action_permissions:
                    raise ClientPackError(
                        "project permission metadata and process definitions disagree"
                    )
                if journey.completion_requirements != definition.completion_requirements:
                    raise ClientPackError(
                        "project completion metadata and process definitions disagree"
                    )
                if [item.model_dump(mode="json") for item in journey.facts] != [
                    item.model_dump(mode="json") for item in definition.facts
                ]:
                    raise ClientPackError("project fact metadata and process definitions disagree")
            scenario_action_types = {
                step.reference
                for journey in self.project.journeys
                for scenario in journey.scenarios
                for step in scenario.steps
                if step.kind == "action"
            }
            if set(simulation_bindings) != scenario_action_types:
                raise ClientPackError(
                    "simulation bindings do not cover the executable scenario actions"
                )
            if any(
                getattr(adapter, "is_simulation_adapter", False) is not True
                for adapter in simulation_bindings.values()
            ):
                raise ClientPackError("simulation bindings must be explicitly marked safe")
            if any(
                not isinstance(getattr(adapter, "id", None), str)
                or not isinstance(getattr(adapter, "guarantees_idempotency", None), bool)
                or not callable(getattr(adapter, "execute", None))
                or not callable(getattr(adapter, "lookup", None))
                for adapter in simulation_bindings.values()
            ):
                raise ClientPackError("simulation bindings must implement the action adapter port")
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

        canonical_composition = {
            "schema_version": 3,
            "manifest": self.manifest.model_dump(mode="json"),
            "definitions": [
                definition.model_dump(mode="json")
                for definition in sorted(definitions, key=lambda item: (item.id, item.version))
            ],
            "agent_decision_output": {
                "type": (
                    f"{self.agent_decision_output_type.__module__}."
                    f"{self.agent_decision_output_type.__qualname__}"
                ),
                "json_schema": self.agent_decision_output_type.model_json_schema(),
            },
            "policy_ids": sorted(policy_ids),
            "action_bindings": {
                action_type: {
                    "adapter_id": adapter.id,
                    "guarantees_idempotency": adapter.guarantees_idempotency,
                }
                for action_type, adapter in sorted(bindings.items())
            },
            "simulation_bindings": {
                action_type: {
                    "adapter_id": adapter.id,
                    "guarantees_idempotency": adapter.guarantees_idempotency,
                }
                for action_type, adapter in sorted(simulation_bindings.items())
            },
            "project": self.project.model_dump(mode="json") if self.project is not None else None,
        }
        canonical_json = json.dumps(
            canonical_composition,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = sha256(canonical_json.encode()).hexdigest()
        object.__setattr__(self, "_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "compatibility",
            DeploymentCompatibility(
                client_pack_fingerprint=fingerprint,
                extension_manifest_hash=self.manifest.fingerprint(),
                definition_fingerprints={
                    (definition.id, definition.version): definition.fingerprint()
                    for definition in definitions
                },
            ),
        )
        registry = ProcessDefinitionRegistry(definitions)
        # Resolve every published trigger now so ambiguous configuration fails
        # before API traffic or worker polling begins.
        published_events = {
            event_type
            for definition in definitions
            if definition.status is DefinitionStatus.PUBLISHED
            for event_type in definition.trigger_events
        }
        for event_type in published_events:
            registry.resolve_trigger(event_type)
        object.__setattr__(self, "registry", registry)

    @property
    def definition(self) -> ProcessDefinition:
        """Convenience accessor for the common single-process client pack."""
        if len(self.definitions) != 1:
            raise ClientPackError("client pack contains more than one process definition")
        return self.definitions[0]

    def fingerprint(self) -> str:
        """Return the deterministic identity of the complete runtime composition."""

        return self._fingerprint

    def trigger_rules(self, release: DeploymentRelease) -> dict[str, ProcessBootstrap]:
        release.require_client_pack(self.fingerprint())
        rules: dict[str, ProcessBootstrap] = {}
        for definition in self.definitions:
            if definition.status is not DefinitionStatus.PUBLISHED:
                continue
            bootstrap = ProcessBootstrap(
                process_type=definition.id,
                definition_version=definition.version,
                extension_manifest_hash=self.manifest.fingerprint(),
                client_pack_fingerprint=self.fingerprint(),
                process_definition_fingerprint=definition.fingerprint(),
                deployment_id=release.deployment_id,
                deployment_release_fingerprint=release.release_fingerprint,
                temporal_task_queue=release.temporal_task_queue,
            )
            for event_type in definition.trigger_events:
                if event_type in rules:
                    raise ClientPackError(f"ambiguous client-pack trigger: {event_type}")
                rules[event_type] = bootstrap
        return rules
