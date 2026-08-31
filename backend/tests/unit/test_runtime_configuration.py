import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError
from tiramisu_agents.api.events import fictional_trigger_rules
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.builtin.fictional_agent_output import FictionalAgentDecisionOutput
from tiramisu_agents.extensions import ClientPack, ExtensionManifest
from tiramisu_agents.processes.definitions import DefinitionStatus, ProcessDefinition
from tiramisu_agents.temporal import worker as worker_module
from tiramisu_agents.temporal.worker import (
    compose_fictional_worker,
    resolve_worker_tenants,
    serve,
)


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


@pytest.fixture(autouse=True)
def clear_relevant_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "OPENAI_API_KEY",
        "TIRAMISU_OPENAI_API_KEY",
        "TIRAMISU_OPENAI_MODEL",
        "TIRAMISU_WORKER_TENANT_IDS",
    ):
        monkeypatch.delenv(variable, raising=False)


def test_fictional_deployment_is_cwd_independent_and_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deployment = load_fictional_deployment()
    trigger = fictional_trigger_rules()["enquiry.created"]

    assert deployment.definition.id == "enquiry_to_booking"
    assert set(deployment.bindings) == set(deployment.definition.allowed_actions)
    assert deployment.agent_decision_output_type is FictionalAgentDecisionOutput
    assert trigger.process_type == deployment.definition.id
    assert trigger.definition_version == deployment.definition.version
    assert trigger.extension_manifest_hash == deployment.manifest.fingerprint()
    assert trigger.client_pack_fingerprint == deployment.fingerprint()
    assert trigger.process_definition_fingerprint == deployment.definition.fingerprint()
    assert len(deployment.fingerprint()) == 64


def test_client_pack_fingerprint_covers_runtime_composition_and_only_published_triggers() -> None:
    deployment = load_fictional_deployment()
    equivalent = ClientPack(
        manifest=deployment.manifest,
        definitions=deployment.definitions,
        bindings=dict(reversed(tuple(deployment.bindings.items()))),
        agent_decision_output_type=deployment.agent_decision_output_type,
        policy_ids=tuple(reversed(deployment.policy_ids)),
    )
    changed_definition = deployment.definition.model_copy(
        update={"goals": (*deployment.definition.goals, "Exercise changed behavior.")}
    )
    changed = ClientPack(
        manifest=deployment.manifest,
        definitions=(changed_definition,),
        bindings=deployment.bindings,
        agent_decision_output_type=deployment.agent_decision_output_type,
        policy_ids=deployment.policy_ids,
    )

    assert equivalent.fingerprint() == deployment.fingerprint()
    assert changed.fingerprint() != deployment.fingerprint()

    for status in (DefinitionStatus.DRAFT, DefinitionStatus.RETIRED):
        disabled = ClientPack(
            manifest=deployment.manifest,
            definitions=(deployment.definition.model_copy(update={"status": status}),),
            bindings=deployment.bindings,
            agent_decision_output_type=deployment.agent_decision_output_type,
            policy_ids=deployment.policy_ids,
        )
        assert disabled.trigger_rules() == {}


def test_bundled_fictional_configuration_matches_public_example() -> None:
    deployment = load_fictional_deployment()
    public_definition = ProcessDefinition.model_validate(
        yaml.safe_load(
            Path("process_definitions/examples/enquiry_to_booking.v1.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    public_manifest = ExtensionManifest.model_validate(
        json.loads(
            Path("examples/fictional_client_pack/extension_manifest.json").read_text(
                encoding="utf-8"
            )
        )
    )

    assert deployment.definition == public_definition
    assert deployment.manifest == public_manifest


def test_settings_normalize_and_reject_unsafe_runtime_combinations() -> None:
    settings = _settings(
        openai_model="   ",
        client_pack_factory="   ",
        log_level="warning",
    )
    assert settings.openai_model is None
    assert settings.openai_api_key is None
    assert settings.client_pack_factory is None
    assert settings.log_level == "WARNING"

    with pytest.raises(ValidationError, match="unsafe development identity"):
        _settings(
            environment="production",
            allow_unsafe_development_tenant_header=True,
        )
    with pytest.raises(ValidationError, match="fictional client pack"):
        _settings(
            environment="staging",
            load_fictional_example_processes=True,
        )
    with pytest.raises(ValidationError, match="task queue"):
        _settings(temporal_task_queue="not a queue!")
    with pytest.raises(ValidationError, match="either TIRAMISU_CLIENT_PACK_FACTORY"):
        _settings(
            client_pack_factory="example:create",
            load_fictional_example_processes=True,
        )
    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg"):
        _settings(database_url="sqlite+aiosqlite:///tiramisu.db")


def test_example_environment_is_a_valid_settings_source() -> None:
    settings = Settings(**cast(Any, {"_env_file": ".env.example"}))

    assert settings.environment == "development"
    assert settings.worker_tenant_ids == ()
    assert settings.openai_model is None
    assert settings.openai_api_key is None


def test_worker_assignments_are_explicit_and_cli_replaces_environment() -> None:
    configured_tenant = uuid4()
    cli_tenant = uuid4()
    settings = _settings(worker_tenant_ids=(configured_tenant,))

    assert resolve_worker_tenants(settings) == (configured_tenant,)
    assert resolve_worker_tenants(settings, (cli_tenant,)) == (cli_tenant,)
    with pytest.raises(ValueError, match="at least one"):
        resolve_worker_tenants(_settings())
    with pytest.raises(ValueError, match="unique"):
        resolve_worker_tenants(settings, (cli_tenant, cli_tenant))


def test_standard_openai_key_and_json_worker_assignments_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-key")
    monkeypatch.setenv("TIRAMISU_WORKER_TENANT_IDS", f'["{tenant_id}"]')

    settings = _settings()

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "environment-test-key"
    assert settings.worker_tenant_ids == (tenant_id,)


def test_fictional_worker_fails_fast_without_model_or_explicit_key() -> None:
    with pytest.raises(ValueError, match="TIRAMISU_OPENAI_MODEL"):
        compose_fictional_worker(_settings())
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        compose_fictional_worker(_settings(openai_model="test-model"))

    deployment = compose_fictional_worker(
        _settings(
            openai_model="test-model",
            openai_api_key="test-key-not-used",
        )
    )
    assert deployment.definition.id == "enquiry_to_booking"


@pytest.mark.asyncio
async def test_worker_validates_composition_before_external_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_engine_creation(*args: object, **kwargs: object) -> None:
        pytest.fail("database engine created before worker configuration was validated")

    async def unexpected_temporal_connection(*args: object, **kwargs: object) -> None:
        pytest.fail("Temporal connected before worker configuration was validated")

    monkeypatch.setattr(worker_module, "create_engine", unexpected_engine_creation)
    monkeypatch.setattr(
        "tiramisu_agents.temporal.worker.Client.connect",
        unexpected_temporal_connection,
    )

    with pytest.raises(ValueError, match="TIRAMISU_OPENAI_MODEL"):
        await serve(
            (uuid4(),),
            settings=_settings(load_fictional_example_processes=True),
        )
