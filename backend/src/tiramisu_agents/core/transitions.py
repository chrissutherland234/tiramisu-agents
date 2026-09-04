"""Infrastructure-free projection rules shared by persistence and simulations."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from tiramisu_agents.core.contracts.actions import ActionRequestStatus
from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    DecisionStatus,
    HumanWakeCondition,
    WakeCondition,
)
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.processes import ProcessStatus


class ProcessTransitionRejected(ValueError):
    """Raised when a validated decision cannot produce a safe process state."""


@dataclass(frozen=True, slots=True)
class ProjectedProcessTransition:
    status: ProcessStatus
    wake_conditions: tuple[WakeCondition, ...]


def apply_fact_observations(
    observations: Iterable[FactObservation],
    *,
    source_type: str,
    source_id: UUID,
    authoritative: dict[str, object],
    claims: dict[str, object],
    provenance: dict[str, dict[str, object]],
) -> None:
    """Apply trusted integration observations to an in-memory projection."""

    for observation in observations:
        target = authoritative if observation.kind is FactKind.AUTHORITATIVE else claims
        target[observation.key] = observation.model_dump(mode="json")["value"]
        provenance[f"{observation.kind.value}:{observation.key}"] = {
            "kind": observation.kind.value,
            "source_type": source_type,
            "source_id": str(source_id),
        }


def project_process_transition(
    *,
    decision: AgentDecision,
    open_actions: tuple[tuple[UUID, ActionRequestStatus], ...],
    terminal_states: frozenset[ProcessStatus] | None,
    authoritative_facts: Mapping[str, object],
    completion_requirements: Mapping[str, Any],
) -> ProjectedProcessTransition:
    """Project status and wakes using the same deterministic production rules."""

    status = _next_status(
        decision=decision,
        open_actions=open_actions,
        terminal_states=terminal_states,
        authoritative_facts=authoritative_facts,
        completion_requirements=completion_requirements,
    )
    return ProjectedProcessTransition(
        status=status,
        wake_conditions=_effective_wake_conditions(
            decision=decision,
            status=status,
            open_actions=open_actions,
        ),
    )


def _next_status(
    *,
    decision: AgentDecision,
    open_actions: tuple[tuple[UUID, ActionRequestStatus], ...],
    terminal_states: frozenset[ProcessStatus] | None,
    authoritative_facts: Mapping[str, object],
    completion_requirements: Mapping[str, Any],
) -> ProcessStatus:
    open_statuses = {status for _, status in open_actions}
    if decision.status is DecisionStatus.COMPLETED and open_actions:
        raise ProcessTransitionRejected("completed decision has unresolved actions")
    if any(
        value
        in {
            ActionRequestStatus.PENDING_APPROVAL,
            ActionRequestStatus.UNKNOWN,
            ActionRequestStatus.RECONCILING,
        }
        for value in open_statuses
    ):
        return ProcessStatus.REVIEW
    if open_statuses:
        return ProcessStatus.ACTIVE
    if decision.status is DecisionStatus.COMPLETED:
        unsatisfied = tuple(
            key
            for key, expected in sorted(completion_requirements.items())
            if key not in authoritative_facts or authoritative_facts[key] != expected
        )
        if unsatisfied:
            raise ProcessTransitionRejected(
                "completion requirements are not satisfied: " + ", ".join(unsatisfied)
            )
        if terminal_states is not None and ProcessStatus.COMPLETED not in terminal_states:
            raise ProcessTransitionRejected("completed is not a configured terminal state")
        return ProcessStatus.COMPLETED
    if decision.status is DecisionStatus.ESCALATED or any(
        isinstance(wake, HumanWakeCondition) for wake in decision.wake_conditions
    ):
        return ProcessStatus.REVIEW
    if decision.status is DecisionStatus.WAITING:
        return ProcessStatus.WAITING
    return ProcessStatus.ACTIVE


def _effective_wake_conditions(
    *,
    decision: AgentDecision,
    status: ProcessStatus,
    open_actions: tuple[tuple[UUID, ActionRequestStatus], ...],
) -> tuple[WakeCondition, ...]:
    open_statuses = {action_status for _, action_status in open_actions}
    if ActionRequestStatus.PENDING_APPROVAL in open_statuses:
        return (HumanWakeCondition(interaction="approval"),)
    if open_statuses & {
        ActionRequestStatus.UNKNOWN,
        ActionRequestStatus.RECONCILING,
    }:
        return (HumanWakeCondition(interaction="operator"),)
    if open_actions:
        return ()
    if status in {
        ProcessStatus.COMPLETED,
        ProcessStatus.CANCELLED,
        ProcessStatus.FAILED,
    }:
        return ()
    if status is ProcessStatus.REVIEW and not decision.wake_conditions:
        return (HumanWakeCondition(interaction="operator"),)
    return decision.wake_conditions
