from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tiramisu_agents.api.events import fictional_trigger_rules
from tiramisu_agents.api.settings import Settings
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.extensions import ClientPack, ClientPackError
from tiramisu_agents.processes.compatibility import DeploymentCompatibilityError
from tiramisu_agents.processes.definitions import DefinitionStatus
from tiramisu_agents.projects import GeneratedAgentDecisionOutput
from tiramisu_agents.temporal import worker as worker_module
from tiramisu_agents.temporal.worker import (
    compose_fictional_worker,
    compose_worker_client_pack,
    resolve_worker_tenants,
    serve,
)
from tiramisu_agents.testkit import make_test_deployment_release


def _settings(**values: object) -> Settings:
    return Settings(**cast(Any, {"_env_file": None, **values}))


@pytest.fixture(autouse=True)
def clear_relevant_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "OPENAI_API_KEY",
        "TIRAMISU_OPENAI_API_KEY",
        "TIRAMISU_OPENAI_MODEL",
        "TIRAMISU_DEPLOYMENT_ID",
        "TIRAMISU_DEPLOYMENT_BUILD_ID",
        "TIRAMISU_DEPLOYMENT_TENANT_IDS",
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
    assert issubclass(deployment.agent_decision_output_type, GeneratedAgentDecisionOutput)
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
        project=deployment.project,
        simulation_bindings=deployment.simulation_bindings,
    )
    changed_definition = deployment.definition.model_copy(
        update={"goals": (*deployment.definition.goals, "Exercise changed behavior.")}
    )
    assert deployment.project is not None
    changed_journey = deployment.project.journeys[0].model_copy(
        update={"goals": changed_definition.goals}
    )
    changed_project = deployment.project.model_copy(update={"journeys": (changed_journey,)})
    changed = ClientPack(
        manifest=deployment.manifest,
        definitions=(changed_definition,),
        bindings=deployment.bindings,
        agent_decision_output_type=deployment.agent_decision_output_type,
        policy_ids=deployment.policy_ids,
        project=changed_project,
        simulation_bindings=deployment.simulation_bindings,
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
        release = make_test_deployment_release(client_pack_fingerprint=disabled.fingerprint())
        assert disabled.trigger_rules(release) == {}


def test_client_pack_rejects_business_metadata_that_disagrees_with_runtime() -> None:
    deployment = load_fictional_deployment()
    assert deployment.project is not None

    with pytest.raises(ClientPackError, match="project and manifest"):
        ClientPack(
            manifest=deployment.manifest,
            definitions=deployment.definitions,
            bindings=deployment.bindings,
            agent_decision_output_type=deployment.agent_decision_output_type,
            policy_ids=deployment.policy_ids,
            project=deployment.project.model_copy(update={"id": "different_project"}),
        )


def test_deployment_release_identity_is_deterministic_and_fails_closed() -> None:
    release = make_test_deployment_release()
    repeated = make_test_deployment_release()
    next_build = make_test_deployment_release(build_id="next-build")

    assert repeated == release
    assert len(release.release_fingerprint) == 64
    assert release.temporal_task_queue == (
        f"tiramisu.{release.deployment_id}.{release.release_fingerprint}"
    )
    assert next_build.release_fingerprint != release.release_fingerprint
    assert next_build.temporal_task_queue != release.temporal_task_queue

    release.require_process(
        deployment_id=release.deployment_id,
        deployment_release_fingerprint=release.release_fingerprint,
        temporal_task_queue=release.temporal_task_queue,
    )
    with pytest.raises(DeploymentCompatibilityError, match="deployment release"):
        release.require_process(
            deployment_id=release.deployment_id,
            deployment_release_fingerprint=next_build.release_fingerprint,
            temporal_task_queue=release.temporal_task_queue,
        )
    with pytest.raises(ValueError, match="deployment ID"):
        make_test_deployment_release(deployment_id="INVALID deployment")
    with pytest.raises(ValueError, match="reserved"):
        make_test_deployment_release(deployment_id="unassigned")


def test_bundled_fictional_configuration_is_self_consistent() -> None:
    deployment = load_fictional_deployment()

    assert deployment.registry.get("enquiry_to_booking", "1") == deployment.definition
    assert deployment.manifest.process_definitions == ("enquiry_to_booking.v1",)


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
    assert settings.deployment_tenant_ids == ()
    assert settings.openai_model is None
    assert settings.openai_api_key is None


def test_worker_assignments_are_explicit_and_cli_replaces_environment() -> None:
    configured_tenant = uuid4()
    cli_tenant = uuid4()
    settings = _settings(deployment_tenant_ids=(configured_tenant,))

    assert resolve_worker_tenants(settings) == (configured_tenant,)
    assert resolve_worker_tenants(settings, (cli_tenant,)) == (cli_tenant,)
    with pytest.raises(ValueError, match="at least one"):
        resolve_worker_tenants(_settings())
    with pytest.raises(ValueError, match="unique"):
        resolve_worker_tenants(settings, (cli_tenant, cli_tenant))

    deployment = compose_worker_client_pack(
        _settings(
            load_fictional_example_processes=True,
            openai_model="test-model",
            openai_api_key="test-key-not-used",
            deployment_id="fictional-test",
            deployment_build_id="cli-test",
        ),
        tenant_ids=(cli_tenant,),
    )
    assert deployment is not None


def test_standard_openai_key_and_json_worker_assignments_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-key")
    monkeypatch.setenv("TIRAMISU_WORKER_TENANT_IDS", f'["{tenant_id}"]')

    settings = _settings()

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "environment-test-key"
    assert settings.deployment_tenant_ids == (tenant_id,)


def test_fictional_worker_fails_fast_without_model_or_explicit_key() -> None:
    with pytest.raises(ValueError, match="TIRAMISU_OPENAI_MODEL"):
        compose_fictional_worker(_settings())
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        compose_fictional_worker(_settings(openai_model="test-model"))
    with pytest.raises(ValueError, match="TIRAMISU_DEPLOYMENT_ID"):
        compose_fictional_worker(
            _settings(openai_model="test-model", openai_api_key="test-key-not-used")
        )
    with pytest.raises(ValueError, match="TIRAMISU_DEPLOYMENT_BUILD_ID"):
        compose_fictional_worker(
            _settings(
                openai_model="test-model",
                openai_api_key="test-key-not-used",
                deployment_id="fictional-test",
            )
        )
    with pytest.raises(ValueError, match="TIRAMISU_DEPLOYMENT_TENANT_IDS"):
        compose_fictional_worker(
            _settings(
                openai_model="test-model",
                openai_api_key="test-key-not-used",
                deployment_id="fictional-test",
                deployment_build_id="unit-test",
            )
        )

    deployment = compose_fictional_worker(
        _settings(
            openai_model="test-model",
            openai_api_key="test-key-not-used",
            deployment_id="fictional-test",
            deployment_build_id="unit-test",
            deployment_tenant_ids=(uuid4(),),
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
