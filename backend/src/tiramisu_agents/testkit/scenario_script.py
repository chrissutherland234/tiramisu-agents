"""Shared interpretation of one compiled client-project scenario."""

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

from pydantic import ValidationError

from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    TimerWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.core.contracts.processes import AgentTurnInput, ProcessSnapshot
from tiramisu_agents.extensions import ClientPack
from tiramisu_agents.extensions.project_metadata import (
    JourneyDescription,
    ScenarioDescription,
    ScenarioStepDescription,
)
from tiramisu_agents.projects.contracts import (
    ScenarioAction,
    ScenarioEvent,
    ScenarioEventWait,
    ScenarioTimerWait,
)
from tiramisu_agents.projects.output import GeneratedAgentDecisionOutput

SCENARIO_NAMESPACE = UUID("9ed12c20-aac2-5cd2-9918-c484baa38938")


class ScenarioRunError(ValueError):
    """Raised when an executable scenario contradicts runtime behavior."""


class CompiledScenarioScript:
    """Resolve events, decisions, values, and assertions from compiled scenario data.

    This interpreter is deliberately infrastructure-free. Both the fast kernel driver
    and the PostgreSQL/Temporal driver use it, so the cross-layer test cannot acquire a
    second, hand-written version of the client's journey logic.
    """

    def __init__(
        self,
        client_pack: ClientPack,
        scenario_id: str,
        *,
        run_identity: str | None = None,
    ) -> None:
        if client_pack.project is None:
            raise ScenarioRunError("client pack has no conventional project scenarios")
        self.pack = client_pack
        self.journey, self.scenario = self._find_scenario(scenario_id)
        base_identity = f"{client_pack.fingerprint()}:{self.journey.id}:{self.scenario.id}"
        self.identity = run_identity or base_identity

    @property
    def definition(self) -> Any:
        return self.pack.registry.get(self.journey.id, self.journey.version)

    def deterministic_uuid(self, value: str) -> UUID:
        return uuid5(SCENARIO_NAMESPACE, f"{self.identity}:{value}")

    def build_event(
        self,
        *,
        step: ScenarioStepDescription,
        step_index: int,
        tenant_id: UUID,
        process_id: UUID | None,
        occurred_at: datetime,
        snapshot: ProcessSnapshot,
    ) -> CanonicalEvent:
        data = ScenarioEvent.model_validate(step.value)
        facts = tuple(
            FactObservation(
                key=fact.key,
                kind=fact.kind,
                value=self.resolve(fact.value, snapshot, step=step),
            )
            for fact in data.facts
        )
        return CanonicalEvent(
            event_id=self.deterministic_uuid(f"event:{step_index}:{step.reference}"),
            tenant_id=tenant_id,
            process_instance_id=process_id,
            event_type=cast(str, step.reference),
            source=data.source,
            source_event_id=f"{self.scenario.id}:{step_index}:{step.reference}",
            occurred_at=occurred_at,
            received_at=occurred_at,
            external_references=data.external_references,
            facts=facts,
            payload=cast(dict[str, Any], self.resolve(data.payload, snapshot, step=step)),
        )

    def build_decision(
        self,
        *,
        step: ScenarioStepDescription,
        step_index: int,
        turn_input: AgentTurnInput,
        now: datetime,
    ) -> AgentDecision:
        payload: dict[str, Any]
        resolution_snapshot = self.prospective_snapshot(turn_input)
        if step.kind == "action":
            action = ScenarioAction.model_validate(step.value)
            payload = {
                "status": DecisionStatus.ACTIVE.value,
                "actions": [
                    {
                        "logical_action_key": action.logical_action_key,
                        "action_type": step.reference,
                        "parameters": self.resolve(
                            action.parameters,
                            resolution_snapshot,
                            step=step,
                        ),
                        "rationale": action.rationale,
                    }
                ],
            }
        elif step.kind == "wait":
            wait = self.parse_wait(step)
            wake: dict[str, Any]
            if isinstance(wait, ScenarioEventWait):
                wake = {"type": "event", "event_type": wait.event_type}
            else:
                wake = {
                    "type": "timer",
                    "at": (now + timedelta(seconds=wait.delay_seconds)).isoformat(),
                }
            payload = {"status": DecisionStatus.WAITING.value, "wake_conditions": [wake]}
        else:
            payload = {"status": DecisionStatus.COMPLETED.value}
        try:
            output = self.pack.agent_decision_output_type.model_validate(payload)
        except ValidationError as error:
            raise self.error(step, f"scripted decision is invalid: {error}") from error
        if not isinstance(output, GeneratedAgentDecisionOutput):
            raise ScenarioRunError("client pack output type is not a generated project output")
        decision = output.to_agent_decision(turn_input)
        actions = tuple(
            action.model_copy(
                update={
                    "action_request_id": self.deterministic_uuid(
                        f"action:{step_index}:{position}:{action.logical_action_key}"
                    )
                }
            )
            for position, action in enumerate(decision.actions)
        )
        return decision.model_copy(
            update={
                "decision_id": self.deterministic_uuid(f"decision:{step_index}"),
                "actions": actions,
            }
        )

    @staticmethod
    def prospective_snapshot(turn_input: AgentTurnInput) -> ProcessSnapshot:
        """Expose current wake-source observations to the deterministic script."""

        authoritative = dict(turn_input.process.authoritative_facts)
        claims = dict(turn_input.process.customer_claims)
        for source in (*turn_input.events, *turn_input.action_results):
            for fact in source.facts:
                target = authoritative if fact.kind.value == "authoritative" else claims
                target[fact.key] = fact.model_dump(mode="json")["value"]
        return turn_input.process.model_copy(
            update={"authoritative_facts": authoritative, "customer_claims": claims}
        )

    def require_expected_action(
        self,
        step: ScenarioStepDescription,
        decision: AgentDecision,
        expected: ScenarioAction,
        *,
        expected_parameters: dict[str, Any],
    ) -> None:
        if len(decision.actions) != 1:
            raise self.error(
                step,
                f"scripted decision produced {len(decision.actions)} actions; expected exactly one",
            )
        action = decision.actions[0]
        if (
            decision.status is not DecisionStatus.ACTIVE
            or decision.wake_conditions
            or action.action_type != step.reference
            or action.logical_action_key != expected.logical_action_key
            or action.parameters != expected_parameters
            or action.rationale != expected.rationale
        ):
            raise self.error(step, "generated action changed the scripted action identity")

    def require_expected_wait(
        self,
        step: ScenarioStepDescription,
        decision: AgentDecision,
        *,
        now: datetime,
    ) -> None:
        wait = self.parse_wait(step)
        if isinstance(wait, ScenarioEventWait):
            expected = EventWakeCondition(event_type=wait.event_type)
        else:
            expected = TimerWakeCondition(at=now + timedelta(seconds=wait.delay_seconds))
        expected_document = expected.model_dump(mode="json")
        actual_documents = tuple(wake.model_dump(mode="json") for wake in decision.wake_conditions)
        if decision.status is not DecisionStatus.WAITING or actual_documents != (
            expected_document,
        ):
            raise self.error(step, "generated wake plan changed the scripted wait")

    @staticmethod
    def parse_wait(step: ScenarioStepDescription) -> ScenarioEventWait | ScenarioTimerWait:
        value = cast(dict[str, Any], step.value)
        if value.get("type") == "event":
            return ScenarioEventWait.model_validate(value)
        return ScenarioTimerWait.model_validate(value)

    def resolve(
        self,
        value: Any,
        snapshot: ProcessSnapshot,
        *,
        step: ScenarioStepDescription,
    ) -> Any:
        if isinstance(value, dict):
            document = cast(dict[str, Any], value)
            if document.get("type") == "fact" and set(document).issubset(
                {"type", "fact_key", "path"}
            ):
                key = cast(str, document.get("fact_key"))
                if key in snapshot.authoritative_facts:
                    resolved: Any = snapshot.authoritative_facts[key]
                elif key in snapshot.customer_claims:
                    resolved = snapshot.customer_claims[key]
                else:
                    raise self.error(step, f"fact is not available yet: {key}")
                path = cast(list[str | int], document.get("path", []))
                for part in path:
                    try:
                        resolved = resolved[part]
                    except (KeyError, IndexError, TypeError) as error:
                        raise self.error(step, f"fact path is unavailable: {key} {path}") from error
                return resolved
            return {key: self.resolve(item, snapshot, step=step) for key, item in document.items()}
        if isinstance(value, list):
            items = cast(list[Any], value)
            return [self.resolve(item, snapshot, step=step) for item in items]
        return value

    def assert_fact_value(
        self,
        step: ScenarioStepDescription,
        snapshot: ProcessSnapshot,
    ) -> Any:
        key = cast(str, step.reference)
        if key in snapshot.authoritative_facts:
            actual = snapshot.authoritative_facts[key]
        elif key in snapshot.customer_claims:
            actual = snapshot.customer_claims[key]
        else:
            raise self.error(step, f"expected fact is absent: {key}")
        if actual != step.value:
            raise self.error(step, f"fact {key} is {actual!r}; expected {step.value!r}")
        return actual

    def error(self, step: ScenarioStepDescription, message: str) -> ScenarioRunError:
        return ScenarioRunError(f"scenario {self.scenario.id} step {step.description!r}: {message}")

    def _find_scenario(self, scenario_id: str) -> tuple[JourneyDescription, ScenarioDescription]:
        assert self.pack.project is not None
        matches = [
            (journey, scenario)
            for journey in self.pack.project.journeys
            for scenario in journey.scenarios
            if scenario.id == scenario_id
        ]
        if not matches:
            available = sorted(
                scenario.id
                for journey in self.pack.project.journeys
                for scenario in journey.scenarios
            )
            choices = ", ".join(available) or "none"
            raise ScenarioRunError(
                f"unknown scenario {scenario_id!r}; available scenarios: {choices}"
            )
        if len(matches) > 1:
            raise ScenarioRunError(
                f"scenario ID {scenario_id!r} is ambiguous across project journeys"
            )
        return matches[0]
