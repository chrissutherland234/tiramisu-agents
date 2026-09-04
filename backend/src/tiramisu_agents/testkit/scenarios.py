"""Executable, infrastructure-free client-project scenarios."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID, uuid5

from pydantic import ValidationError

from tiramisu_agents.core.action_identity import (
    action_payload_identity,
    execution_idempotency_key,
)
from tiramisu_agents.core.action_policy import initial_action_request_status
from tiramisu_agents.core.contracts.actions import (
    ActionAttemptStatus,
    ActionRequestStatus,
    PermissionOutcome,
)
from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    TimerWakeCondition,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactObservation
from tiramisu_agents.core.contracts.processes import (
    ActionResultContext,
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
)
from tiramisu_agents.core.limits import require_process_fact_projection
from tiramisu_agents.core.policy import DecisionRejected, validate_decision
from tiramisu_agents.core.ports.actions import ProviderActionRequest
from tiramisu_agents.core.transitions import (
    ProcessTransitionRejected,
    apply_fact_observations,
    project_process_transition,
)
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

_SCENARIO_NAMESPACE = UUID("9ed12c20-aac2-5cd2-9918-c484baa38938")


class ScenarioRunError(ValueError):
    """Raised when an executable scenario contradicts runtime behavior."""


class ScenarioTraceKind(StrEnum):
    EVENT = "event"
    DECISION = "decision"
    ACTION = "action"
    APPROVAL = "approval"
    RESULT = "result"
    WAKE = "wake"
    FACT = "fact"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ScenarioTraceEntry:
    sequence: int
    occurred_at: datetime
    kind: ScenarioTraceKind
    description: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "occurred_at": self.occurred_at.isoformat(),
            "kind": self.kind.value,
            "description": self.description,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    project_id: str
    journey_id: str
    scenario_id: str
    title: str
    final_status: ProcessStatus
    authoritative_facts: dict[str, Any]
    customer_claims: dict[str, Any]
    action_types: tuple[str, ...]
    approval_count: int
    trace: tuple[ScenarioTraceEntry, ...]

    @property
    def passed(self) -> bool:
        return self.final_status is ProcessStatus.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "journey_id": self.journey_id,
            "scenario_id": self.scenario_id,
            "title": self.title,
            "passed": self.passed,
            "final_status": self.final_status.value,
            "authoritative_facts": self.authoritative_facts,
            "customer_claims": self.customer_claims,
            "action_types": list(self.action_types),
            "approval_count": self.approval_count,
            "trace": [entry.as_dict() for entry in self.trace],
        }

    def render(self) -> str:
        lines = [
            f"PASS: {self.title} [{self.scenario_id}]",
            f"Journey: {self.journey_id}",
        ]
        for entry in self.trace:
            lines.append(f"  {entry.sequence}. {entry.kind.value}: {entry.description}")
        lines.extend(
            (
                f"Final status: {self.final_status.value}",
                f"Actions: {len(self.action_types)}; approvals: {self.approval_count}",
            )
        )
        return "\n".join(lines)


class ScenarioDriver(Protocol):
    """Execution boundary for running the same scenario against another runtime layer."""

    async def run(self, scenario_id: str) -> ScenarioResult: ...


class KernelScenarioDriver:
    """Run compiled scenario data through production policies and shared transitions."""

    def __init__(self, client_pack: ClientPack) -> None:
        if client_pack.project is None:
            raise ScenarioRunError("client pack has no conventional project scenarios")
        self._pack = client_pack

    async def run(self, scenario_id: str) -> ScenarioResult:
        journey, scenario = self._find_scenario(scenario_id)
        definition = self._pack.registry.get(journey.id, journey.version)
        identity = f"{self._pack.fingerprint()}:{journey.id}:{scenario.id}"
        tenant_id = uuid5(_SCENARIO_NAMESPACE, f"{identity}:tenant")
        process_id = uuid5(_SCENARIO_NAMESPACE, f"{identity}:process")
        now = scenario.started_at
        snapshot = ProcessSnapshot(
            tenant_id=tenant_id,
            process_instance_id=process_id,
            process_type=journey.id,
            process_definition_version=journey.version,
            status=ProcessStatus.ACTIVE,
        )
        trace: list[ScenarioTraceEntry] = []
        action_types: list[str] = []
        approval_count = 0
        pending_events: tuple[CanonicalEvent, ...] = ()
        pending_results: tuple[ActionResultContext, ...] = ()
        pending_timers: tuple[str, ...] = ()
        index = 0
        turn_number = 0

        while index < len(scenario.steps):
            if not (pending_events or pending_results or pending_timers):
                step = scenario.steps[index]
                if step.kind != "event":
                    raise self._error(scenario, step, "expected a business event wake source")
                event = self._build_event(
                    scenario=scenario,
                    step=step,
                    step_index=index,
                    tenant_id=tenant_id,
                    process_id=process_id,
                    occurred_at=now,
                    snapshot=snapshot,
                )
                if index > 0 and not any(
                    getattr(wake, "event_type", None) == event.event_type
                    for wake in snapshot.current_wake_conditions
                ):
                    raise self._error(
                        scenario,
                        step,
                        f"process is not waiting for {event.event_type}",
                    )
                pending_events = (event,)
                self._append_trace(
                    trace,
                    now,
                    ScenarioTraceKind.EVENT,
                    step.description,
                    {"event_type": event.event_type},
                )
                index += 1

            assertions: list[ScenarioStepDescription] = []
            while index < len(scenario.steps) and scenario.steps[index].kind == "fact":
                assertions.append(scenario.steps[index])
                index += 1
            if index >= len(scenario.steps):
                raise ScenarioRunError(f"scenario {scenario.id} ends without an agent decision")

            decision_step = scenario.steps[index]
            if decision_step.kind not in {"action", "wait", "complete"}:
                raise self._error(
                    scenario,
                    decision_step,
                    "expected an action, wait, or completion decision",
                )
            turn_number += 1
            turn_id = uuid5(_SCENARIO_NAMESPACE, f"{identity}:turn:{turn_number}")
            turn_input = AgentTurnInput(
                turn_id=turn_id,
                process=snapshot,
                events=pending_events,
                action_results=pending_results,
                timer_ids=pending_timers,
                instructions=definition.compile_instructions(),
            )
            decision = self._build_decision(
                scenario=scenario,
                step=decision_step,
                step_index=index,
                turn_input=turn_input,
                now=now,
                identity=identity,
            )
            prospective_authoritative = dict(snapshot.authoritative_facts)
            for source in (*pending_events, *pending_results):
                for fact in source.facts:
                    if fact.kind.value == "authoritative":
                        prospective_authoritative[fact.key] = fact.model_dump(mode="json")["value"]
            try:
                validate_decision(
                    decision,
                    definition.decision_policy(),
                    workflow_now=now,
                    expected_event_ids=frozenset(event.event_id for event in pending_events),
                    expected_action_attempt_ids=frozenset(
                        result.attempt_id for result in pending_results
                    ),
                    expected_timer_ids=frozenset(pending_timers),
                    current_authoritative_facts=prospective_authoritative,
                )
            except DecisionRejected as error:
                raise self._error(scenario, decision_step, str(error)) from error

            action_spec = (
                ScenarioAction.model_validate(decision_step.value)
                if decision_step.kind == "action"
                else None
            )
            if action_spec is not None:
                expected_parameters = cast(
                    dict[str, Any],
                    self._resolve(
                        action_spec.parameters,
                        self._prospective_snapshot(turn_input),
                        scenario=scenario,
                        step=decision_step,
                    ),
                )
                self._require_expected_action(
                    scenario,
                    decision_step,
                    decision,
                    action_spec,
                    expected_parameters=expected_parameters,
                )
            elif decision_step.kind == "wait":
                self._require_expected_wait(
                    scenario,
                    decision_step,
                    decision,
                    now=now,
                )
            self._append_trace(
                trace,
                now,
                ScenarioTraceKind.DECISION,
                decision_step.description,
                {
                    "status": decision.status.value,
                    "actions": [action.action_type for action in decision.actions],
                    "wake_conditions": [
                        wake.model_dump(mode="json") for wake in decision.wake_conditions
                    ],
                },
            )

            authoritative = dict(snapshot.authoritative_facts)
            claims = dict(snapshot.customer_claims)
            provenance = dict(snapshot.fact_provenance)
            for event in pending_events:
                apply_fact_observations(
                    event.facts,
                    source_type="event",
                    source_id=event.event_id,
                    authoritative=authoritative,
                    claims=claims,
                    provenance=provenance,
                )
            for result in pending_results:
                apply_fact_observations(
                    result.facts,
                    source_type="action_attempt",
                    source_id=result.attempt_id,
                    authoritative=authoritative,
                    claims=claims,
                    provenance=provenance,
                )
            try:
                require_process_fact_projection(
                    authoritative_facts=authoritative,
                    customer_claims=claims,
                    fact_provenance=provenance,
                )
            except ValueError as error:
                raise self._error(scenario, decision_step, str(error)) from error

            classified: list[tuple[Any, PermissionOutcome, ActionRequestStatus]] = []
            open_actions: list[tuple[UUID, ActionRequestStatus]] = []
            for action in decision.actions:
                permission = definition.action_policy().evaluate(action).outcome
                status = initial_action_request_status(permission)
                classified.append((action, permission, status))
                if status is not ActionRequestStatus.DENIED:
                    open_actions.append((action.action_request_id, status))
                self._append_trace(
                    trace,
                    now,
                    ScenarioTraceKind.ACTION,
                    f"Proposed {action.action_type}",
                    {
                        "permission": permission.value,
                        "logical_action_key": action.logical_action_key,
                    },
                )
            try:
                transition = project_process_transition(
                    decision=decision,
                    open_actions=tuple(open_actions),
                    terminal_states=frozenset(definition.terminal_states),
                    authoritative_facts=authoritative,
                    completion_requirements=definition.completion_requirements,
                )
            except ProcessTransitionRejected as error:
                raise self._error(scenario, decision_step, str(error)) from error

            snapshot = snapshot.model_copy(
                update={
                    "status": transition.status,
                    "authoritative_facts": authoritative,
                    "customer_claims": claims,
                    "fact_provenance": provenance,
                    "memory_summary": decision.memory_update.summary,
                    "memory_summary_source_event_ids": (
                        decision.memory_update.summary_source_event_ids
                    ),
                    "memory_summary_source_review_command_ids": (
                        decision.memory_update.summary_source_review_command_ids
                    ),
                    "memory_summary_source_action_attempt_ids": (
                        decision.memory_update.summary_source_action_attempt_ids
                    ),
                    "memory_summary_source_timer_ids": (
                        decision.memory_update.summary_source_timer_ids
                    ),
                    "open_commitments": decision.memory_update.open_commitments,
                    "current_wake_conditions": transition.wake_conditions,
                    "state_version": snapshot.state_version + 1,
                }
            )
            for assertion in assertions:
                self._assert_fact(scenario, assertion, snapshot, trace, now)

            pending_events = ()
            pending_results = ()
            pending_timers = ()
            index += 1

            if decision_step.kind == "action":
                assert action_spec is not None
                results: list[ActionResultContext] = []
                for action, permission, _ in classified:
                    if permission is PermissionOutcome.REQUIRE_APPROVAL:
                        if not action_spec.approve:
                            raise self._error(
                                scenario,
                                decision_step,
                                f"{action.action_type} requires an explicit scenario approval",
                            )
                        approval_count += 1
                        self._append_trace(
                            trace,
                            now,
                            ScenarioTraceKind.APPROVAL,
                            f"Approved {action.action_type}",
                            {"logical_action_key": action.logical_action_key},
                        )
                    elif action_spec.approve:
                        raise self._error(
                            scenario,
                            decision_step,
                            f"{action.action_type} does not require approval",
                        )
                    if permission is PermissionOutcome.DENY:
                        raise self._error(
                            scenario,
                            decision_step,
                            f"policy denied {action.action_type}",
                        )
                    result = await self._execute_action(
                        action=action,
                        tenant_id=tenant_id,
                        process_id=process_id,
                        authoritative_facts=authoritative,
                        turn_number=turn_number,
                        identity=identity,
                    )
                    results.append(result)
                    action_types.append(action.action_type)
                    self._append_trace(
                        trace,
                        now,
                        ScenarioTraceKind.RESULT,
                        f"{action.action_type} succeeded",
                        {"provider_reference": result.provider_reference},
                    )
                pending_results = tuple(results)
            elif decision_step.kind == "wait":
                wait = self._parse_wait(decision_step)
                self._append_trace(
                    trace,
                    now,
                    ScenarioTraceKind.WAKE,
                    decision_step.description,
                    wait.model_dump(mode="json"),
                )
                if isinstance(wait, ScenarioTimerWait):
                    now += timedelta(seconds=wait.delay_seconds)
                    pending_timers = (wait.timer_id,)
            else:
                self._append_trace(
                    trace,
                    now,
                    ScenarioTraceKind.COMPLETE,
                    decision_step.description,
                    {"status": snapshot.status.value},
                )

        if snapshot.status is not ProcessStatus.COMPLETED:
            raise ScenarioRunError(
                f"scenario {scenario.id} ended in {snapshot.status.value}, not completed"
            )
        project = self._pack.project
        assert project is not None
        return ScenarioResult(
            project_id=project.id,
            journey_id=journey.id,
            scenario_id=scenario.id,
            title=scenario.title,
            final_status=snapshot.status,
            authoritative_facts=dict(snapshot.authoritative_facts),
            customer_claims=dict(snapshot.customer_claims),
            action_types=tuple(action_types),
            approval_count=approval_count,
            trace=tuple(trace),
        )

    def _find_scenario(self, scenario_id: str) -> tuple[JourneyDescription, ScenarioDescription]:
        assert self._pack.project is not None
        matches = [
            (journey, scenario)
            for journey in self._pack.project.journeys
            for scenario in journey.scenarios
            if scenario.id == scenario_id
        ]
        if not matches:
            available = sorted(
                scenario.id
                for journey in self._pack.project.journeys
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

    def _build_event(
        self,
        *,
        scenario: ScenarioDescription,
        step: ScenarioStepDescription,
        step_index: int,
        tenant_id: UUID,
        process_id: UUID,
        occurred_at: datetime,
        snapshot: ProcessSnapshot,
    ) -> CanonicalEvent:
        data = ScenarioEvent.model_validate(step.value)
        facts = tuple(
            FactObservation(
                key=fact.key,
                kind=fact.kind,
                value=self._resolve(fact.value, snapshot, scenario=scenario, step=step),
            )
            for fact in data.facts
        )
        return CanonicalEvent(
            event_id=uuid5(
                _SCENARIO_NAMESPACE,
                f"{scenario.id}:event:{step_index}:{step.reference}",
            ),
            tenant_id=tenant_id,
            process_instance_id=process_id,
            event_type=cast(str, step.reference),
            source=data.source,
            source_event_id=f"{scenario.id}:{step_index}:{step.reference}",
            occurred_at=occurred_at,
            received_at=occurred_at,
            external_references=data.external_references,
            facts=facts,
            payload=cast(
                dict[str, Any], self._resolve(data.payload, snapshot, scenario=scenario, step=step)
            ),
        )

    def _build_decision(
        self,
        *,
        scenario: ScenarioDescription,
        step: ScenarioStepDescription,
        step_index: int,
        turn_input: AgentTurnInput,
        now: datetime,
        identity: str,
    ) -> AgentDecision:
        payload: dict[str, Any]
        resolution_snapshot = self._prospective_snapshot(turn_input)
        if step.kind == "action":
            action = ScenarioAction.model_validate(step.value)
            payload = {
                "status": DecisionStatus.ACTIVE.value,
                "actions": [
                    {
                        "logical_action_key": action.logical_action_key,
                        "action_type": step.reference,
                        "parameters": self._resolve(
                            action.parameters,
                            resolution_snapshot,
                            scenario=scenario,
                            step=step,
                        ),
                        "rationale": action.rationale,
                    }
                ],
            }
        elif step.kind == "wait":
            wait = self._parse_wait(step)
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
            output = self._pack.agent_decision_output_type.model_validate(payload)
        except ValidationError as error:
            raise self._error(scenario, step, f"scripted decision is invalid: {error}") from error
        if not isinstance(output, GeneratedAgentDecisionOutput):
            raise ScenarioRunError("client pack output type is not a generated project output")
        decision = output.to_agent_decision(turn_input)
        actions = tuple(
            action.model_copy(
                update={
                    "action_request_id": uuid5(
                        _SCENARIO_NAMESPACE,
                        f"{identity}:action:{step_index}:{position}:{action.logical_action_key}",
                    )
                }
            )
            for position, action in enumerate(decision.actions)
        )
        return decision.model_copy(
            update={
                "decision_id": uuid5(
                    _SCENARIO_NAMESPACE,
                    f"{identity}:decision:{step_index}",
                ),
                "actions": actions,
            }
        )

    @staticmethod
    def _prospective_snapshot(turn_input: AgentTurnInput) -> ProcessSnapshot:
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

    @staticmethod
    def _require_expected_action(
        scenario: ScenarioDescription,
        step: ScenarioStepDescription,
        decision: AgentDecision,
        expected: ScenarioAction,
        *,
        expected_parameters: dict[str, Any],
    ) -> None:
        if len(decision.actions) != 1:
            raise ScenarioRunError(
                f"scenario {scenario.id} step {step.description!r}: scripted decision produced "
                f"{len(decision.actions)} actions; expected exactly one"
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
            raise ScenarioRunError(
                f"scenario {scenario.id} step {step.description!r}: generated action changed "
                "the scripted action identity"
            )

    @staticmethod
    def _require_expected_wait(
        scenario: ScenarioDescription,
        step: ScenarioStepDescription,
        decision: AgentDecision,
        *,
        now: datetime,
    ) -> None:
        wait = KernelScenarioDriver._parse_wait(step)
        if isinstance(wait, ScenarioEventWait):
            expected = EventWakeCondition(event_type=wait.event_type)
        else:
            expected = TimerWakeCondition(at=now + timedelta(seconds=wait.delay_seconds))
        expected_document = expected.model_dump(mode="json")
        actual_documents = tuple(wake.model_dump(mode="json") for wake in decision.wake_conditions)
        if decision.status is not DecisionStatus.WAITING or actual_documents != (
            expected_document,
        ):
            raise ScenarioRunError(
                f"scenario {scenario.id} step {step.description!r}: generated wake plan "
                "changed the scripted wait"
            )

    async def _execute_action(
        self,
        *,
        action: Any,
        tenant_id: UUID,
        process_id: UUID,
        authoritative_facts: dict[str, Any],
        turn_number: int,
        identity: str,
    ) -> ActionResultContext:
        try:
            adapter = self._pack.simulation_bindings[action.action_type]
        except KeyError as error:
            raise ScenarioRunError(
                f"scenario action has no explicitly safe simulation adapter: {action.action_type}"
            ) from error
        payload_hash = action_payload_identity(action.action_type, action.parameters)
        key = execution_idempotency_key(
            tenant_id,
            process_id,
            action.action_request_id,
            1,
            payload_hash,
        )
        try:
            provider_result = await adapter.execute(
                ProviderActionRequest(
                    action_type=action.action_type,
                    parameters=action.parameters,
                    idempotency_key=key,
                    tenant_id=tenant_id,
                    process_instance_id=process_id,
                    authoritative_facts=authoritative_facts,
                )
            )
        except Exception as error:
            raise ScenarioRunError(
                f"scenario provider rejected {action.action_type}: {type(error).__name__}: {error}"
            ) from error
        return ActionResultContext(
            attempt_id=uuid5(
                _SCENARIO_NAMESPACE,
                f"{identity}:attempt:{turn_number}:{action.logical_action_key}",
            ),
            action_request_id=action.action_request_id,
            revision=1,
            action_type=action.action_type,
            parameters=action.parameters,
            status=ActionAttemptStatus.SUCCEEDED,
            adapter_id=adapter.id,
            idempotency_key=key,
            provider_reference=provider_result.provider_reference,
            result=provider_result.result,
            facts=provider_result.facts,
        )

    @staticmethod
    def _parse_wait(step: ScenarioStepDescription) -> ScenarioEventWait | ScenarioTimerWait:
        value = cast(dict[str, Any], step.value)
        if value.get("type") == "event":
            return ScenarioEventWait.model_validate(value)
        return ScenarioTimerWait.model_validate(value)

    def _resolve(
        self,
        value: Any,
        snapshot: ProcessSnapshot,
        *,
        scenario: ScenarioDescription,
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
                    raise self._error(scenario, step, f"fact is not available yet: {key}")
                path = cast(list[str | int], document.get("path", []))
                for part in path:
                    try:
                        resolved = resolved[part]
                    except (KeyError, IndexError, TypeError) as error:
                        raise self._error(
                            scenario,
                            step,
                            f"fact path is unavailable: {key} {path}",
                        ) from error
                return resolved
            return {
                key: self._resolve(item, snapshot, scenario=scenario, step=step)
                for key, item in document.items()
            }
        if isinstance(value, list):
            items = cast(list[Any], value)
            return [self._resolve(item, snapshot, scenario=scenario, step=step) for item in items]
        return value

    def _assert_fact(
        self,
        scenario: ScenarioDescription,
        step: ScenarioStepDescription,
        snapshot: ProcessSnapshot,
        trace: list[ScenarioTraceEntry],
        now: datetime,
    ) -> None:
        key = cast(str, step.reference)
        if key in snapshot.authoritative_facts:
            actual = snapshot.authoritative_facts[key]
        elif key in snapshot.customer_claims:
            actual = snapshot.customer_claims[key]
        else:
            raise self._error(scenario, step, f"expected fact is absent: {key}")
        if actual != step.value:
            raise self._error(
                scenario,
                step,
                f"fact {key} is {actual!r}; expected {step.value!r}",
            )
        self._append_trace(
            trace,
            now,
            ScenarioTraceKind.FACT,
            step.description,
            {"fact_key": key, "value": actual},
        )

    @staticmethod
    def _append_trace(
        trace: list[ScenarioTraceEntry],
        occurred_at: datetime,
        kind: ScenarioTraceKind,
        description: str,
        details: dict[str, Any],
    ) -> None:
        trace.append(
            ScenarioTraceEntry(
                sequence=len(trace) + 1,
                occurred_at=occurred_at,
                kind=kind,
                description=description,
                details=details,
            )
        )

    @staticmethod
    def _error(
        scenario: ScenarioDescription,
        step: ScenarioStepDescription,
        message: str,
    ) -> ScenarioRunError:
        return ScenarioRunError(f"scenario {scenario.id} step {step.description!r}: {message}")


class ScenarioRunner:
    """Select a named scenario and delegate it to a pluggable runtime driver."""

    def __init__(
        self,
        client_pack: ClientPack,
        *,
        driver: ScenarioDriver | None = None,
    ) -> None:
        self._driver = driver or KernelScenarioDriver(client_pack)

    async def run(self, scenario_id: str) -> ScenarioResult:
        return await self._driver.run(scenario_id)


async def run_scenario(client_pack: ClientPack, scenario_id: str) -> ScenarioResult:
    """Convenience entry point used by client-project tests and the CLI."""

    return await ScenarioRunner(client_pack).run(scenario_id)


def scenario_result_json(result: ScenarioResult) -> str:
    return json.dumps(result.as_dict(), indent=2, sort_keys=True)
