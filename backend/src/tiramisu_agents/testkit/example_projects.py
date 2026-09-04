"""Small client projects for exercising reusable scenario-driver contracts."""

from datetime import UTC, datetime, timedelta
from typing import Literal

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
    Route,
    Scenario,
    ScenarioStep,
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


def create_timer_project(*, expected_status: str = "completed") -> Project:
    """Create a one-hour timer scenario with one safe provider action."""

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
