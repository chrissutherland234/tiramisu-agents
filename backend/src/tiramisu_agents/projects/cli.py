"""Author commands for creating, validating, and explaining client projects."""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

from agents.agent_output import AgentOutputSchema

from tiramisu_agents.extensions import ClientPack, ClientPackError
from tiramisu_agents.projects.contracts import Project, ProjectConfigurationError
from tiramisu_agents.projects.scaffold import create_project_scaffold
from tiramisu_agents.testkit.scenario_script import ScenarioRunError
from tiramisu_agents.testkit.scenarios import ScenarioRunner, scenario_result_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tiramisu",
        description="Create and inspect conventional Tiramisu client projects",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("startproject", help="create a conventional client project")
    start.add_argument("name", help="snake_case project and package name")
    start.add_argument(
        "directory",
        nargs="?",
        type=Path,
        help="target directory; defaults to a new directory named after the project",
    )
    check = commands.add_parser("check", help="compile and validate a project")
    check.add_argument("target", help="Project, ClientPack, or factory as module:attribute")
    describe = commands.add_parser("describe", help="explain a project in business terms")
    describe.add_argument("target", help="Project, ClientPack, or factory as module:attribute")
    describe.add_argument("--json", action="store_true", help="emit compiled metadata as JSON")
    simulate = commands.add_parser(
        "simulate", help="run a deterministic executable project scenario"
    )
    simulate.add_argument("target", help="Project, ClientPack, or factory as module:attribute")
    simulate.add_argument("--scenario", required=True, help="scenario ID to execute")
    simulate.add_argument("--json", action="store_true", help="emit the result and trace as JSON")
    return parser


def load_project_target(target: str) -> ClientPack:
    module_name, separator, attribute_name = target.partition(":")
    if not separator or not module_name.strip() or not attribute_name.strip():
        raise ProjectConfigurationError("project target must use the form 'module:attribute'")
    if ":" in attribute_name:
        raise ProjectConfigurationError("project target must contain exactly one ':' separator")
    try:
        module = import_module(module_name)
    except ImportError as error:
        raise ProjectConfigurationError(
            f"could not import project module {module_name!r}"
        ) from error
    try:
        candidate: Any = getattr(module, attribute_name)
    except AttributeError as error:
        raise ProjectConfigurationError(
            f"project module {module_name!r} has no attribute {attribute_name!r}"
        ) from error
    value: Any = candidate() if callable(candidate) else candidate
    if isinstance(value, Project):
        return value.compile()
    if isinstance(value, ClientPack):
        return value
    raise ProjectConfigurationError("project target did not resolve to a Project or ClientPack")


def check_project(target: str) -> str:
    pack = load_project_target(target)
    AgentOutputSchema(pack.agent_decision_output_type).json_schema()
    project_name = pack.project.title if pack.project is not None else pack.manifest.extension_id
    return (
        f"OK: {project_name} compiles to {len(pack.definitions)} journey(s), "
        f"{len(pack.bindings)} capability binding(s); fingerprint {pack.fingerprint()}"
    )


def describe_project(target: str, *, as_json: bool = False) -> str:
    pack = load_project_target(target)
    if pack.project is None:
        return _describe_low_level_pack(pack, as_json=as_json)
    if as_json:
        return pack.project.model_dump_json(indent=2)
    lines = [
        f"{pack.project.title} ({pack.project.id} {pack.project.version})",
        pack.project.description,
    ]
    for journey in pack.project.journeys:
        lines.extend(("", f"Journey: {journey.title}", f"  {journey.description}"))
        lines.append("  Goals:")
        lines.extend(f"    - {goal}" for goal in journey.goals)
        starts = [
            f"{route.title} [{route.event_type}]"
            for route in journey.routes
            if route.kind == "start"
        ]
        wakes = [
            f"{route.title} [{route.event_type}]"
            for route in journey.routes
            if route.kind == "wake"
        ]
        lines.append(f"  Starts on: {', '.join(starts) or 'nothing'}")
        lines.append(f"  Wakes on: {', '.join(wakes) or 'timers only'}")
        if journey.capabilities:
            lines.append("  Can:")
            for capability in journey.capabilities:
                permission = journey.permissions[capability.action_type].value.replace("_", " ")
                lines.append(f"    - {capability.title} ({capability.action_type}; {permission})")
        else:
            lines.append("  Can: observe only; no business actions")
        lines.append("  Completes when:")
        facts = {fact.key: fact for fact in journey.facts}
        for key, expected in sorted(journey.completion_requirements.items()):
            lines.append(f"    - {facts[key].title} is {json.dumps(expected)}")
        for scenario in journey.scenarios:
            lines.append(f"  Example: {scenario.title}")
            lines.extend(
                f"    {index}. {step.description}"
                for index, step in enumerate(scenario.steps, start=1)
            )
    return "\n".join(lines)


async def simulate_project(
    target: str,
    *,
    scenario_id: str,
    as_json: bool = False,
) -> str:
    result = await ScenarioRunner(load_project_target(target)).run(scenario_id)
    return scenario_result_json(result) if as_json else result.render()


def _describe_low_level_pack(pack: ClientPack, *, as_json: bool) -> str:
    document = {
        "id": pack.manifest.extension_id,
        "version": pack.manifest.extension_version,
        "journeys": [definition.model_dump(mode="json") for definition in pack.definitions],
    }
    if as_json:
        return json.dumps(document, indent=2)
    return (
        f"{pack.manifest.extension_id} {pack.manifest.extension_version}\n"
        f"Compiled low-level client pack with {len(pack.definitions)} journey(s)."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "startproject":
            directory = arguments.directory or Path.cwd() / arguments.name
            files = create_project_scaffold(arguments.name, directory.resolve())
            print(f"Created {arguments.name} in {directory.resolve()} ({len(files)} files)")
            return 0
        if arguments.command == "check":
            print(check_project(arguments.target))
            return 0
        if arguments.command == "describe":
            print(describe_project(arguments.target, as_json=arguments.json))
            return 0
        print(
            asyncio.run(
                simulate_project(
                    arguments.target,
                    scenario_id=arguments.scenario,
                    as_json=arguments.json,
                )
            )
        )
        return 0
    except (
        ClientPackError,
        ProjectConfigurationError,
        ScenarioRunError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def run() -> None:
    raise SystemExit(main())
