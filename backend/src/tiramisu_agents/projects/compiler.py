"""Compile conventional projects into the existing safety-fenced ClientPack runtime."""

from collections.abc import Iterable
from typing import Any, cast

from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.extensions import ClientPack, ExtensionManifest
from tiramisu_agents.extensions.project_metadata import (
    CapabilityDescription,
    FactDescription,
    JourneyDescription,
    ProjectDescription,
    RouteDescription,
    ScenarioDescription,
    ScenarioStepDescription,
)
from tiramisu_agents.processes.definitions import (
    CommunicationConfiguration,
    FactDefinition,
    ProcessDefinition,
    ReviewConfiguration,
)
from tiramisu_agents.projects.contracts import (
    Capability,
    DecisionTransformer,
    Fact,
    Journey,
    Project,
    ProjectConfigurationError,
    Route,
    Scenario,
    ScenarioAction,
    ScenarioEvent,
    ScenarioEventWait,
    ScenarioTimerWait,
    ScenarioValue,
)
from tiramisu_agents.projects.output import generate_agent_decision_output_type


def _unique_by(items: Iterable[Any], attribute: str, *, label: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for item in items:
        key = getattr(item, attribute)
        if key in indexed:
            raise ProjectConfigurationError(f"duplicate {label}: {key}")
        indexed[key] = item
    return indexed


def _fact_description(fact: Fact) -> FactDescription:
    return FactDescription(
        key=fact.key,
        title=fact.title,
        description=fact.description,
        kinds=fact.kinds,
        value_schema=fact.value_schema,
        operator_editable=fact.operator_editable,
    )


def _fact_definition(fact: Fact) -> FactDefinition:
    return FactDefinition.model_validate(_fact_description(fact).model_dump(mode="json"))


def _capability_description(capability: Capability) -> CapabilityDescription:
    return CapabilityDescription(
        action_type=capability.action_type,
        title=capability.title,
        description=capability.description,
        adapter_id=capability.adapter.id,
        default_permission=capability.default_permission,
        parameters_schema=capability.parameters_model.model_json_schema(),
        produces_fact_keys=tuple(fact.key for fact in capability.produces),
    )


def _route_description(route: Route) -> RouteDescription:
    return RouteDescription(
        kind=route.kind,
        event_type=route.event_type,
        title=route.title,
        description=route.description,
        provides_fact_keys=tuple(fact.key for fact in route.provides),
    )


def _scenario_description(scenario: Scenario) -> ScenarioDescription:
    return ScenarioDescription(
        id=scenario.id,
        title=scenario.title,
        description=scenario.description,
        steps=tuple(
            ScenarioStepDescription(
                kind=step.kind,
                description=step.description,
                reference=step.reference,
                value=(
                    step.value.model_dump(mode="json")
                    if hasattr(step.value, "model_dump")
                    else step.value
                ),
            )
            for step in scenario.steps
        ),
        started_at=scenario.started_at,
    )


def _collect_fact_catalog(project: Project) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    for fact in (
        *project.facts,
        *(fact for capability in project.capabilities for fact in capability.produces),
        *(fact for route in project.routes for fact in route.provides),
    ):
        existing = facts.get(fact.key)
        if existing is not None and _fact_description(existing) != _fact_description(fact):
            raise ProjectConfigurationError(
                f"fact {fact.key} has inconsistent definitions across the project"
            )
        facts[fact.key] = fact
    return facts


def _validate_scenario(
    scenario: Scenario,
    *,
    journey: Journey,
    routes: tuple[Route, ...],
    facts: dict[str, Fact],
    capabilities: dict[str, Capability],
) -> None:
    starts = {route.event_type for route in routes if route.kind == "start"}
    wakes = {route.event_type for route in routes if route.kind == "wake"}
    routes_by_event = {route.event_type: route for route in routes}
    if scenario.steps[0].kind != "event" or scenario.steps[0].reference not in starts:
        raise ProjectConfigurationError(
            f"scenario {scenario.id} must begin with a start event for journey {journey.id}"
        )
    if scenario.steps[-1].kind != "complete":
        raise ProjectConfigurationError(f"scenario {scenario.id} must end with completion")

    expected_event: str | None = None
    turn_ready = False
    for index, step in enumerate(scenario.steps):
        if step.kind == "event" and step.reference not in routes_by_event:
            raise ProjectConfigurationError(
                f"scenario {scenario.id} references an event outside journey {journey.id}: "
                f"{step.reference}"
            )
        if step.kind == "event":
            event_type = cast(str, step.reference)
            event = ScenarioEvent.model_validate(step.value)
            if index > 0 and expected_event != step.reference:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} event {step.reference} must follow a matching wait"
                )
            expected_event = None
            turn_ready = True
            route = routes_by_event[event_type]
            provided = {fact.key: fact for fact in route.provides}
            if len({item.key for item in event.facts}) != len(event.facts):
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} event {step.reference} repeats a fact"
                )
            for observation in event.facts:
                fact = facts.get(observation.key)
                if fact is None or observation.key not in provided:
                    raise ProjectConfigurationError(
                        f"scenario {scenario.id} event {step.reference} supplies an undeclared "
                        f"fact: {observation.key}"
                    )
                if observation.kind not in fact.kinds:
                    raise ProjectConfigurationError(
                        f"scenario {scenario.id} event {step.reference} supplies {observation.key} "
                        f"with an unsupported authority kind"
                    )
                _validate_scenario_value_references(
                    observation.value,
                    scenario=scenario,
                    facts=facts,
                )
            _validate_scenario_value_references(event.payload, scenario=scenario, facts=facts)
        elif step.kind == "action":
            action_type = cast(str, step.reference)
            if not turn_ready:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} action {step.reference} has no wake source"
                )
            if step.reference not in journey.capabilities:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} references an unavailable action: {step.reference}"
                )
            action = ScenarioAction.model_validate(step.value)
            capability = capabilities[action_type]
            has_references = _validate_scenario_value_references(
                action.parameters,
                scenario=scenario,
                facts=facts,
            )
            if not has_references:
                try:
                    capability.parameters_model.model_validate(action.parameters)
                except ValueError as error:
                    raise ProjectConfigurationError(
                        f"scenario {scenario.id} has invalid {step.reference} parameters: {error}"
                    ) from error
            permission = journey.permission_overrides.get(
                step.reference, capability.default_permission
            )
            if action.approve and permission is not PermissionOutcome.REQUIRE_APPROVAL:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} approves {step.reference}, but policy does not "
                    "require approval"
                )
            if permission is PermissionOutcome.REQUIRE_APPROVAL and not action.approve:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} must explicitly approve {step.reference}"
                )
            if permission is PermissionOutcome.DENY:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} cannot execute denied action {step.reference}"
                )
            # A successful action result is the next turn's wake source.
            turn_ready = True
        elif step.kind == "wait":
            if not turn_ready:
                raise ProjectConfigurationError(f"scenario {scenario.id} wait has no wake source")
            wait = step.value
            if isinstance(wait, ScenarioEventWait):
                if wait.event_type not in wakes:
                    raise ProjectConfigurationError(
                        f"scenario {scenario.id} waits for an unavailable event: {wait.event_type}"
                    )
                expected_event = wait.event_type
                turn_ready = False
            elif isinstance(wait, ScenarioTimerWait):
                # The deterministic runner advances its fake clock and opens the next turn.
                turn_ready = True
            else:
                raise ProjectConfigurationError(f"scenario {scenario.id} has an invalid wait")
        if step.kind == "fact":
            if not turn_ready:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} fact assertion has no pending wake source"
                )
            if step.reference not in facts:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} references an unknown fact: {step.reference}"
                )
        if step.kind == "complete":
            if not turn_ready:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} completion has no wake source"
                )
            if index != len(scenario.steps) - 1:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} completion must be the final step"
                )
            turn_ready = False
    if expected_event is not None:
        raise ProjectConfigurationError(
            f"scenario {scenario.id} never supplies awaited event {expected_event}"
        )


def _validate_scenario_value_references(
    value: Any,
    *,
    scenario: Scenario,
    facts: dict[str, Fact],
) -> bool:
    """Validate fact references recursively and report whether any were present."""

    if isinstance(value, ScenarioValue):
        if value.fact_key not in facts:
            raise ProjectConfigurationError(
                f"scenario {scenario.id} reads an unknown fact: {value.fact_key}"
            )
        return True
    if hasattr(value, "model_dump"):
        return _validate_scenario_value_references(
            value.model_dump(mode="json"), scenario=scenario, facts=facts
        )
    if isinstance(value, dict):
        document = cast(dict[str, Any], value)
        if document.get("type") == "fact" and set(document).issubset({"type", "fact_key", "path"}):
            fact_key = document.get("fact_key")
            if fact_key not in facts:
                raise ProjectConfigurationError(
                    f"scenario {scenario.id} reads an unknown fact: {fact_key}"
                )
            return True
        found = False
        for item in document.values():
            found = (
                _validate_scenario_value_references(item, scenario=scenario, facts=facts) or found
            )
        return found
    if isinstance(value, (list, tuple)):
        items = cast(list[Any] | tuple[Any, ...], value)
        found = False
        for item in items:
            found = (
                _validate_scenario_value_references(item, scenario=scenario, facts=facts) or found
            )
        return found
    return False


def compile_project(project: Project) -> ClientPack:
    """Validate one project completely and compile its immutable runtime representation."""

    journeys = _unique_by(project.journeys, "id", label="journey ID")
    capabilities = _unique_by(project.capabilities, "action_type", label="capability action type")
    scenarios = _unique_by(project.scenarios, "id", label="scenario ID")
    _unique_by(project.facts, "key", label="project fact key")
    facts = _collect_fact_catalog(project)

    routes_by_journey: dict[str, list[Route]] = {journey_id: [] for journey_id in journeys}
    route_identities: set[tuple[str, str, str]] = set()
    for route in project.routes:
        if route.journey_id not in journeys:
            raise ProjectConfigurationError(
                f"route {route.event_type} references unknown journey {route.journey_id}"
            )
        identity = (route.journey_id, route.kind, route.event_type)
        if identity in route_identities:
            raise ProjectConfigurationError(
                f"journey {route.journey_id} declares duplicate {route.kind} route "
                f"{route.event_type}"
            )
        route_identities.add(identity)
        routes_by_journey[route.journey_id].append(route)

    published_journeys = {
        journey.id for journey in project.journeys if journey.status.value == "published"
    }
    published_starts: set[str] = set()
    for route in project.routes:
        if route.kind != "start" or route.journey_id not in published_journeys:
            continue
        if route.event_type in published_starts:
            raise ProjectConfigurationError(
                f"published start event is routed to more than one journey: {route.event_type}"
            )
        published_starts.add(route.event_type)

    scenarios_by_journey: dict[str, list[Scenario]] = {journey_id: [] for journey_id in journeys}
    for scenario in scenarios.values():
        if scenario.journey_id not in journeys:
            raise ProjectConfigurationError(
                f"scenario {scenario.id} references unknown journey {scenario.journey_id}"
            )
        scenarios_by_journey[scenario.journey_id].append(scenario)

    definitions: list[ProcessDefinition] = []
    journey_descriptions: list[JourneyDescription] = []
    used_action_types: set[str] = set()
    decision_transformers: dict[str, DecisionTransformer] = {}
    policy_ids: list[str] = []
    for journey in project.journeys:
        journey_routes = tuple(routes_by_journey[journey.id])
        start_routes = tuple(route for route in journey_routes if route.kind == "start")
        wake_routes = tuple(route for route in journey_routes if route.kind == "wake")
        if journey.status.value == "published" and not start_routes:
            raise ProjectConfigurationError(
                f"published journey {journey.id} requires at least one start route"
            )
        unknown_capabilities = set(journey.capabilities) - set(capabilities)
        if unknown_capabilities:
            raise ProjectConfigurationError(
                f"journey {journey.id} uses unknown capabilities: "
                + ", ".join(sorted(unknown_capabilities))
            )
        if not set(journey.permission_overrides).issubset(journey.capabilities):
            raise ProjectConfigurationError(
                f"journey {journey.id} has permission overrides for unavailable capabilities"
            )
        if not set(journey.action_guidance).issubset(journey.capabilities):
            raise ProjectConfigurationError(
                f"journey {journey.id} has guidance for unavailable capabilities"
            )
        if not set(journey.communications.outbound_actions).issubset(journey.capabilities):
            raise ProjectConfigurationError(
                f"journey {journey.id} marks an unavailable capability as outbound"
            )
        wake_events = {route.event_type for route in wake_routes}
        communication_events = {
            *journey.communications.customer_reply_events,
            *journey.communications.opt_out_events,
            *journey.communications.automated_response_events,
        }
        if not communication_events.issubset(wake_events):
            raise ProjectConfigurationError(
                f"journey {journey.id} communication events must have wake routes"
            )

        completion_requirements = {
            requirement.fact_key: requirement.expected_value
            for requirement in journey.complete_when
        }
        unknown_completion_facts = set(completion_requirements) - set(facts)
        if unknown_completion_facts:
            raise ProjectConfigurationError(
                f"journey {journey.id} completes on unknown facts: "
                + ", ".join(sorted(unknown_completion_facts))
            )
        if any(FactKind.AUTHORITATIVE not in facts[key].kinds for key in completion_requirements):
            raise ProjectConfigurationError(
                f"journey {journey.id} completion must use authoritative facts"
            )

        selected = tuple(capabilities[action_type] for action_type in journey.capabilities)
        used_action_types.update(journey.capabilities)
        permissions = {
            capability.action_type: journey.permission_overrides.get(
                capability.action_type, capability.default_permission
            )
            for capability in selected
        }
        action_guidance = {
            capability.action_type: journey.action_guidance.get(
                capability.action_type, capability.guidance
            )
            for capability in selected
        }
        relevant_fact_keys = {
            *completion_requirements,
            *(fact.key for capability in selected for fact in capability.produces),
            *(fact.key for route in journey_routes for fact in route.provides),
        }
        relevant_facts = tuple(facts[key] for key in sorted(relevant_fact_keys))
        definition = ProcessDefinition(
            id=journey.id,
            version=journey.version,
            status=journey.status,
            trigger_events=tuple(route.event_type for route in start_routes),
            goals=journey.goals,
            terminal_states=journey.terminal_states,
            allowed_actions=journey.capabilities,
            action_permissions=permissions,
            action_guidance=action_guidance,
            decision_guidance=journey.decision_guidance,
            allowed_wake_events=tuple(route.event_type for route in wake_routes),
            limits=journey.limits,
            review=ReviewConfiguration(commands=journey.review_commands),
            communications=CommunicationConfiguration(
                outbound_action_types=journey.communications.outbound_actions,
                reply_event_types=journey.communications.customer_reply_events,
                opt_out_event_types=journey.communications.opt_out_events,
                automated_response_event_types=(journey.communications.automated_response_events),
                quiet_hours=journey.communications.quiet_hours,
            ),
            integrations={capability.action_type: capability.adapter.id for capability in selected},
            facts=tuple(_fact_definition(fact) for fact in relevant_facts),
            completion_requirements=completion_requirements,
        )
        definitions.append(definition)
        if journey.decision_transformer is not None:
            decision_transformers[journey.id] = journey.decision_transformer
        journey_scenarios = tuple(scenarios_by_journey[journey.id])
        for scenario in journey_scenarios:
            _validate_scenario(
                scenario,
                journey=journey,
                routes=journey_routes,
                facts={fact.key: fact for fact in relevant_facts},
                capabilities=capabilities,
            )
        journey_descriptions.append(
            JourneyDescription(
                id=journey.id,
                version=journey.version,
                title=journey.title,
                description=journey.description,
                status=journey.status.value,
                goals=journey.goals,
                routes=tuple(_route_description(route) for route in journey_routes),
                capabilities=tuple(_capability_description(item) for item in selected),
                facts=tuple(_fact_description(fact) for fact in relevant_facts),
                permissions=permissions,
                limits=journey.limits,
                communications=definition.communications,
                completion_requirements=completion_requirements,
                scenarios=tuple(_scenario_description(item) for item in journey_scenarios),
            )
        )
        policy_ids.append(f"{project.id}.{journey.id}.policy.v{journey.version}")

    unused_capabilities = set(capabilities) - used_action_types
    if unused_capabilities:
        raise ProjectConfigurationError(
            "project registers capabilities unused by any journey: "
            + ", ".join(sorted(unused_capabilities))
        )

    used_capabilities = tuple(
        capability
        for capability in project.capabilities
        if capability.action_type in used_action_types
    )
    scenario_action_types = {
        step.reference
        for scenario in project.scenarios
        for step in scenario.steps
        if step.kind == "action"
    }
    simulation_bindings: dict[str, Any] = {}
    for capability in used_capabilities:
        if capability.action_type not in scenario_action_types:
            continue
        simulation_adapter = capability.simulation_adapter
        if (
            simulation_adapter is None
            and getattr(capability.adapter, "is_simulation_adapter", False) is True
        ):
            simulation_adapter = capability.adapter
        if simulation_adapter is None:
            raise ProjectConfigurationError(
                f"scenario action {capability.action_type} requires an explicitly safe "
                "simulation adapter"
            )
        simulation_bindings[capability.action_type] = simulation_adapter
    output_type = generate_agent_decision_output_type(
        project_id=project.id,
        project_version=project.version,
        capabilities=used_capabilities,
        wake_event_types=tuple(
            sorted({route.event_type for route in project.routes if route.kind == "wake"})
        ),
        decision_transformers=decision_transformers,
    )
    project_description = ProjectDescription(
        id=project.id,
        version=project.version,
        title=project.title,
        description=project.description,
        journeys=tuple(journey_descriptions),
    )
    manifest = ExtensionManifest(
        extension_id=project.id,
        extension_version=project.version,
        tiramisu_compatibility=project.tiramisu_compatibility,
        process_definitions=tuple(
            f"{definition.id}.v{definition.version}" for definition in definitions
        ),
        adapters=tuple(sorted({capability.adapter.id for capability in used_capabilities})),
        policies=tuple(policy_ids),
    )
    return ClientPack(
        manifest=manifest,
        definitions=tuple(definitions),
        bindings={capability.action_type: capability.adapter for capability in used_capabilities},
        agent_decision_output_type=output_type,
        policy_ids=tuple(policy_ids),
        project=project_description,
        simulation_bindings=simulation_bindings,
    )
