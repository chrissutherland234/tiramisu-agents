"""Reusable executable scenario contracts and deterministic runtime."""

from datetime import UTC, datetime

import pytest
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.projects import (
    Capability,
    Project,
    ProjectConfigurationError,
    Scenario,
    ScenarioStep,
)
from tiramisu_agents.testkit import (
    KernelScenarioDriver,
    ScenarioResult,
    ScenarioRunError,
    ScenarioRunner,
    ScenarioTraceKind,
    run_scenario,
)
from tiramisu_agents.testkit.example_projects import create_timer_project


class RecordingScenarioDriver:
    def __init__(self, inner: KernelScenarioDriver) -> None:
        self.inner = inner
        self.scenario_ids: list[str] = []

    async def run(self, scenario_id: str) -> ScenarioResult:
        self.scenario_ids.append(scenario_id)
        return await self.inner.run(scenario_id)


@pytest.mark.asyncio
async def test_scenario_runner_advances_a_fake_timer_and_uses_provider_facts() -> None:
    result = await run_scenario(create_timer_project().compile(), "follow_up")

    assert result.passed is True
    assert result.action_types == ("finish_work",)
    assert result.authoritative_facts == {"work.status": "completed"}
    wake = next(entry for entry in result.trace if entry.kind is ScenarioTraceKind.WAKE)
    action = next(entry for entry in result.trace if entry.kind is ScenarioTraceKind.ACTION)
    assert wake.occurred_at == datetime(2026, 2, 3, 9, tzinfo=UTC)
    assert action.occurred_at == datetime(2026, 2, 3, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_scenario_runner_has_a_runtime_driver_boundary() -> None:
    pack = create_timer_project().compile()
    driver = RecordingScenarioDriver(KernelScenarioDriver(pack))

    result = await ScenarioRunner(pack, driver=driver).run("follow_up")

    assert result.passed is True
    assert driver.scenario_ids == ["follow_up"]


@pytest.mark.asyncio
async def test_scenario_runner_reports_the_exact_failed_fact_expectation() -> None:
    with pytest.raises(
        ScenarioRunError,
        match="fact work.status is 'completed'; expected 'open'",
    ):
        await run_scenario(create_timer_project(expected_status="open").compile(), "follow_up")


def test_scenario_compilation_requires_explicit_approval() -> None:
    project = create_timer_project()
    capability = project.capabilities[0]
    approval_capability = Capability(
        action_type=capability.action_type,
        title=capability.title,
        description=capability.description,
        parameters_model=capability.parameters_model,
        adapter=capability.adapter,
        guidance=capability.guidance,
        default_permission=PermissionOutcome.REQUIRE_APPROVAL,
        produces=capability.produces,
    )

    with pytest.raises(ProjectConfigurationError, match="must explicitly approve finish_work"):
        Project(
            id=project.id,
            version=project.version,
            title=project.title,
            description=project.description,
            journeys=project.journeys,
            routes=project.routes,
            capabilities=(approval_capability,),
            facts=project.facts,
            scenarios=project.scenarios,
        ).compile()


def test_scenario_compilation_rejects_an_event_without_a_matching_wait() -> None:
    project = create_timer_project()
    scenario = project.scenarios[0]
    invalid = Scenario(
        id=scenario.id,
        journey_id=scenario.journey_id,
        title=scenario.title,
        description=scenario.description,
        steps=(
            scenario.steps[0],
            ScenarioStep.event("work.created", "The same start event appears again."),
            scenario.steps[-1],
        ),
    )

    with pytest.raises(ProjectConfigurationError, match="must follow a matching wait"):
        Project(
            id=project.id,
            version=project.version,
            title=project.title,
            description=project.description,
            journeys=project.journeys,
            routes=project.routes,
            capabilities=project.capabilities,
            facts=project.facts,
            scenarios=(invalid,),
        ).compile()


def test_scenario_compilation_never_falls_back_to_an_unmarked_production_adapter() -> None:
    project = create_timer_project()
    capability = project.capabilities[0]
    unsafe_adapter = StubActionAdapter()
    unsafe_adapter.is_simulation_adapter = False
    production_capability = Capability(
        action_type=capability.action_type,
        title=capability.title,
        description=capability.description,
        parameters_model=capability.parameters_model,
        adapter=unsafe_adapter,
        guidance=capability.guidance,
        default_permission=capability.default_permission,
        produces=capability.produces,
    )

    with pytest.raises(ProjectConfigurationError, match="explicitly safe simulation adapter"):
        Project(
            id=project.id,
            version=project.version,
            title=project.title,
            description=project.description,
            journeys=project.journeys,
            routes=project.routes,
            capabilities=(production_capability,),
            facts=project.facts,
            scenarios=project.scenarios,
        ).compile()


@pytest.mark.asyncio
async def test_scenario_uses_the_safe_binding_instead_of_the_production_binding() -> None:
    project = create_timer_project()
    capability = project.capabilities[0]
    simulation_adapter = capability.adapter
    assert isinstance(simulation_adapter, StubActionAdapter)
    production_adapter = StubActionAdapter()
    production_adapter.is_simulation_adapter = False
    split_capability = Capability(
        action_type=capability.action_type,
        title=capability.title,
        description=capability.description,
        parameters_model=capability.parameters_model,
        adapter=production_adapter,
        simulation_adapter=simulation_adapter,
        guidance=capability.guidance,
        default_permission=capability.default_permission,
        produces=capability.produces,
    )
    pack = Project(
        id=project.id,
        version=project.version,
        title=project.title,
        description=project.description,
        journeys=project.journeys,
        routes=project.routes,
        capabilities=(split_capability,),
        facts=project.facts,
        scenarios=project.scenarios,
    ).compile()

    result = await run_scenario(pack, "follow_up")

    assert result.passed is True
    assert production_adapter.requests == []
    assert len(simulation_adapter.requests) == 1
