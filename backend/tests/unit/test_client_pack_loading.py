"""Public client-pack composition and explicit startup loading contracts."""

import sys
from types import ModuleType
from typing import Any, cast

import pytest
from tiramisu_agents.api.main import create_app
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.extensions import ClientPackError, load_client_pack
from tiramisu_agents.temporal.worker import compose_worker_client_pack


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


def test_explicit_factory_loader_rejects_malformed_or_untrusted_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ClientPackError, match="module:attribute"):
        load_client_pack("not-a-factory-path")

    module = ModuleType("test_invalid_client_pack")
    module.create = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ClientPackError, match="did not return a ClientPack"):
        load_client_pack(f"{module.__name__}:create")


def test_client_pack_contract_fails_closed_on_manifest_runtime_drift() -> None:
    valid = load_fictional_deployment()
    invalid_manifest = valid.manifest.model_copy(update={"activities": ("custom.activity",)})

    with pytest.raises(ClientPackError, match="custom Temporal activities"):
        type(valid)(
            manifest=invalid_manifest,
            definitions=valid.definitions,
            bindings=valid.bindings,
            agent_decision_output_type=valid.agent_decision_output_type,
            policy_ids=valid.policy_ids,
        )


def test_api_and_worker_load_the_same_bundled_client_pack_contract() -> None:
    factory_path = "tiramisu_agents.builtin:load_fictional_deployment"
    tenant_id = "5dc839ab-b42e-42e8-a8d9-afc240ce1d94"
    settings = _settings(
        client_pack_factory=factory_path,
        openai_model="gpt-4o-mini",
        openai_api_key="test-key-not-used",
        deployment_id="fictional-test",
        deployment_build_id="unit-test",
        deployment_tenant_ids=(tenant_id,),
    )

    app = create_app(settings=settings)
    worker_pack = compose_worker_client_pack(settings)

    assert app.state.client_pack is not None
    assert worker_pack is not None
    assert app.state.client_pack.manifest == worker_pack.manifest
    assert app.state.trigger_rules == worker_pack.trigger_rules(app.state.deployment_release)
    assert app.state.process_registry.get("enquiry_to_booking", "2") == (
        worker_pack.registry.get("enquiry_to_booking", "2")
    )
    assert set(app.state.client_pack.bindings) == set(worker_pack.bindings)


def test_worker_requires_model_credentials_for_any_configured_client_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("test_valid_client_pack")
    module.create = load_fictional_deployment  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    settings = _settings(client_pack_factory=f"{module.__name__}:create")

    with pytest.raises(ValueError, match="TIRAMISU_OPENAI_MODEL"):
        compose_worker_client_pack(settings)
