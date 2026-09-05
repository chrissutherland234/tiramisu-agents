"""Conventional starter files for a separately editable client project."""

import keyword
import re
from pathlib import Path

from tiramisu_agents.projects.contracts import ProjectConfigurationError


def create_project_scaffold(name: str, directory: Path) -> tuple[Path, ...]:
    """Create a small working project without overwriting any existing files."""

    if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or keyword.iskeyword(name):
        raise ProjectConfigurationError("project name must be a snake_case identifier")
    if directory.exists() and any(directory.iterdir()):
        raise ProjectConfigurationError(f"target directory is not empty: {directory}")

    package = directory / "src" / name
    tests = directory / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    title = name.replace("_", " ").title()
    files = {
        directory / ".gitignore": ".venv/\n__pycache__/\n*.py[cod]\ndist/\n",
        directory / "README.md": _readme(name, title),
        directory / "pyproject.toml": _pyproject(name),
        package / "__init__.py": (
            f'"""{title} Tiramisu client project."""\n\n'
            "from .project import create_client_pack, create_project\n\n"
            '__all__ = ["create_client_pack", "create_project"]\n'
        ),
        package / "project.py": _project_module(name, title),
        tests / "test_project.py": _test_module(name),
    }
    for path, content in files.items():
        if path.exists():
            raise ProjectConfigurationError(f"refusing to overwrite existing file: {path}")
        path.write_text(content, encoding="utf-8")
    return tuple(files)


def _readme(name: str, title: str) -> str:
    return f"""# {title}

This is an editable Tiramisu client project. Its public entry point is
`{name}:create_client_pack`.

The files follow one convention:

- `Project` identifies this client implementation.
- `Journey` describes the goal and deterministic completion facts.
- `Route` says which business events start or wake the journey.
- `Capability` binds one typed business action to an adapter and permission.
- `Fact` names trusted business knowledge.
- `Communications` declares which actions contact customers and the hard contact rules.
- `Scenario` records an executable acceptance example in business language.

If a capability contacts a customer, declare it in `Journey.communications` together with genuine
reply, opt-out, automated-response, quiet-hour, and message-budget rules. Those rules are enforced
outside the model and by the scenario runner.

During local development, install Tiramisu and this project into the same environment:

```console
uv pip install -e /path/to/tiramisu
uv pip install -e .
tiramisu check {name}:create_project
tiramisu describe {name}:create_project
tiramisu simulate {name}:create_project --scenario happy_path
pytest
```

When replacing the stub with a real provider adapter, retain a separate explicitly safe
`simulation_adapter` for executable scenarios.
"""


def _pyproject(name: str) -> str:
    distribution = name.replace("_", "-")
    return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tiramisu-{distribution}"
version = "0.1.0"
requires-python = ">=3.13,<3.15"
dependencies = ["tiramisu-workspace>=0.1,<0.2"]

[tool.hatch.build.targets.wheel]
packages = ["src/{name}"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def _project_module(name: str, title: str) -> str:
    return f'''"""The conventional {title} journey definition."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.ports.actions import ProviderActionResult
from tiramisu_agents.extensions import ClientPack
from tiramisu_agents.projects import (
    Capability,
    Fact,
    Journey,
    Project,
    Route,
    Scenario,
    ScenarioStep,
)


class PerformTaskParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instruction: str = Field(min_length=1, max_length=2_000)


WORK_STATUS = Fact(
    key="work.status",
    title="Work status",
    description="The authoritative status of this piece of work.",
    value_type=Literal["open", "completed"],
    operator_editable=True,
)


def create_project() -> Project:
    perform_task = Capability(
        action_type="perform_task",
        title="Perform task",
        description="Ask the configured business provider to perform the task.",
        parameters_model=PerformTaskParameters,
        adapter=StubActionAdapter(
            (
                ProviderActionResult(
                    provider_reference="scenario-work-1",
                    result={{"completed": True}},
                    facts=(
                        FactObservation(
                            key=WORK_STATUS.key,
                            kind=FactKind.AUTHORITATIVE,
                            value="completed",
                        ),
                    ),
                ),
            )
        ),
        guidance="Use one concrete, bounded instruction.",
    )
    journey = Journey(
        id="manage_work",
        version="1",
        title="Manage one piece of work",
        description="Follow one business item until an authoritative provider completes it.",
        goals=("Complete the requested work safely",),
        capabilities=(perform_task.action_type,),
        complete_when=(WORK_STATUS.equals("completed"),),
    )
    return Project(
        id="{name}",
        version="0.1.0",
        title="{title}",
        description="A durable client journey implemented with Tiramisu.",
        journeys=(journey,),
        routes=(
            Route.start(
                "work.created",
                journey=journey.id,
                title="Work created",
                description="Start an agent when a new piece of work arrives.",
                provides=(WORK_STATUS,),
            ),
            Route.wake(
                "work.completed",
                journey=journey.id,
                title="Work completed",
                description="Wake when the provider authoritatively completes the work.",
                provides=(WORK_STATUS,),
            ),
        ),
        capabilities=(perform_task,),
        facts=(WORK_STATUS,),
        scenarios=(
            Scenario(
                id="happy_path",
                journey_id=journey.id,
                title="Work is completed",
                description="The normal path through the journey.",
                steps=(
                    ScenarioStep.event(
                        "work.created",
                        "A new item starts the journey.",
                        facts=(WORK_STATUS.observed("open"),),
                    ),
                    ScenarioStep.action(
                        "perform_task",
                        "The agent proposes the business task.",
                        parameters={{"instruction": "Complete the requested work"}},
                        approve=True,
                    ),
                    ScenarioStep.wait_for_event(
                        "work.completed", "The agent waits for provider confirmation."
                    ),
                    ScenarioStep.event(
                        "work.completed",
                        "The provider confirms completion.",
                        facts=(WORK_STATUS.observed("completed"),),
                    ),
                    ScenarioStep.fact(
                        WORK_STATUS, "completed", "Completion is authoritative."
                    ),
                    ScenarioStep.complete("The journey completes."),
                ),
            ),
        ),
    )


def create_client_pack() -> ClientPack:
    return create_project().compile()
'''


def _test_module(name: str) -> str:
    return f"""import asyncio
from {name} import create_project
from tiramisu_agents.testkit import run_scenario


def test_project_compiles() -> None:
    pack = create_project().compile()

    assert pack.definition.id == "manage_work"
    assert pack.definition.trigger_events == ("work.created",)
    assert pack.definition.completion_requirements == {{"work.status": "completed"}}


def test_happy_path() -> None:
    result = asyncio.run(run_scenario(create_project().compile(), "happy_path"))

    assert result.passed is True
"""
