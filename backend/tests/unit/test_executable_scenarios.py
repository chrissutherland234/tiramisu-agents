"""Reusable executable scenario contracts and deterministic runtime."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.ports.actions import ProviderActionResult
from tiramisu_agents.projects import (
    Capability,
    Fact,
    Journey,
    Project,
    ProjectConfigurationError,
    Route,
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


class FinishWorkParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instruction: str = Field(min_length=1)


WORK_STATUS = Fact(
    key="work.status",
    title="Work status",
    description="The authoritative state of the work.",
    value_type=Literal["open", "completed"],
)


class RecordingScenarioDriver:
    def __init__(self, inner: KernelScenarioDriver) -> None:
        self.inner = inner
        self.scenario_ids: list[str] = []

    async def run(self, scenario_id: str) -> ScenarioResult:
        self.scenario_ids.append(scenario_id)
        return await self.inner.run(scenario_id)


def _timer_project(*, expected_status: str = "completed") -> Project:
    adapter = StubActionAdapter(
        (
            ProviderActionResult(
                provider_reference="work-1",
                result={"completed": True},
                facts=(
                    FactObservation(
                        key=WORK_STATUS.key,
                        kind=FactKind.AUTHORITATIVE,
                        value="completed",
                    ),
                ),
            ),
        )
    )
    finish = Capability(
        action_type="finish_work",
        title="Finish work",
        description="Complete the business task.",
        parameters_model=FinishWorkParameters,
        adapter=adapter,
        guidance="Use a concrete instruction.",
        default_permission=PermissionOutcome.ALLOW,
        produces=(WORK_STATUS,),
    )
    journey = Journey(
        id="finish_work",
        version="1",
        title="Finish work",
        description="Follow up once and complete the task.",
        goals=("Complete the task",),
        capabilities=(finish.action_type,),
        complete_when=(WORK_STATUS.equals("completed"),),
    )
    return Project(
        id="timer_demo",
        version="1",
        title="Timer demo",
        description="An executable timer scenario.",
        journeys=(journey,),
        routes=(
            Route.start(
                "work.created",
                journey=journey.id,
                title="Work created",
                description="Start the task.",
                provides=(WORK_STATUS,),
            ),
        ),
        capabilities=(finish,),
        facts=(WORK_STATUS,),
        scenarios=(
            Scenario(
                id="follow_up",
                journey_id=journey.id,
                title="Timed follow-up",
                description="The agent wakes after an hour and completes the task.",
                started_at=datetime(2026, 2, 3, 9, tzinfo=UTC),
                steps=(
                    ScenarioStep.event(
                        "work.created",
                        "Work arrives.",
                        facts=(WORK_STATUS.observed("open"),),
                    ),
                    ScenarioStep.wait_for_timer(
                        "follow-up-1",
                        timedelta(hours=1),
                        "The agent schedules a one-hour follow-up.",
                    ),
                    ScenarioStep.action(
                        "finish_work",
                        "The agent completes the work.",
                        parameters={"instruction": "Complete work-1"},
                    ),
                    ScenarioStep.fact(
                        WORK_STATUS,
                        expected_status,
                        "The provider result is authoritative.",
                    ),
                    ScenarioStep.complete("The journey completes."),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_scenario_runner_advances_a_fake_timer_and_uses_provider_facts() -> None:
    result = await run_scenario(_timer_project().compile(), "follow_up")

    assert result.passed is True
    assert result.action_types == ("finish_work",)
    assert result.authoritative_facts == {"work.status": "completed"}
    wake = next(entry for entry in result.trace if entry.kind is ScenarioTraceKind.WAKE)
    action = next(entry for entry in result.trace if entry.kind is ScenarioTraceKind.ACTION)
    assert wake.occurred_at == datetime(2026, 2, 3, 9, tzinfo=UTC)
    assert action.occurred_at == datetime(2026, 2, 3, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_scenario_runner_has_a_runtime_driver_boundary() -> None:
    pack = _timer_project().compile()
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
        await run_scenario(_timer_project(expected_status="open").compile(), "follow_up")


def test_scenario_compilation_requires_explicit_approval() -> None:
    project = _timer_project()
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
    project = _timer_project()
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
    project = _timer_project()
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
    project = _timer_project()
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
