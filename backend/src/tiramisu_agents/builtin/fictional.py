"""One fail-fast composition shared by fictional API ingress and worker execution."""

import json
from importlib.resources import files
from typing import Any

import yaml

from tiramisu_agents.adapters.stubs import StubBusinessState, stub_business_bindings
from tiramisu_agents.builtin.fictional_agent_output import FictionalAgentDecisionOutput
from tiramisu_agents.extensions import ClientPack, ClientPackError, ExtensionManifest
from tiramisu_agents.processes.definitions import ProcessDefinition


class FictionalDeploymentError(ClientPackError):
    """Raised when bundled extension metadata and runtime bindings disagree."""


FictionalDeployment = ClientPack


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
    provider_state = state or StubBusinessState()
    bindings = stub_business_bindings(provider_state)
    try:
        return ClientPack(
            manifest=manifest,
            definitions=(definition,),
            bindings=bindings,
            agent_decision_output_type=FictionalAgentDecisionOutput,
            policy_ids=("fictional.default.v1",),
        )
    except ClientPackError as error:
        raise FictionalDeploymentError(f"invalid fictional client pack: {error}") from error
