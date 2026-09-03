"""Deterministic validation applied after an agent proposes a decision."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any
from uuid import UUID

from tiramisu_agents.core.action_identity import action_payload_identity
from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    HumanWakeCondition,
    TimerWakeCondition,
)
from tiramisu_agents.core.limits import (
    DEFAULT_PLATFORM_SAFETY_LIMITS,
    require_item_count,
    require_json_bytes,
    require_utf8_bytes,
)


class DecisionRejected(ValueError):
    """Raised when a proposal falls outside deterministic process policy."""


def _empty_completion_requirements() -> Mapping[str, Any]:
    return {}


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    allowed_action_types: frozenset[str]
    allowed_wake_event_types: frozenset[str]
    human_wake_action_types: frozenset[str] = frozenset()
    max_actions_per_turn: int = 5
    max_timer_horizon: timedelta = timedelta(days=30)
    max_action_parameter_fields: int = DEFAULT_PLATFORM_SAFETY_LIMITS.max_action_parameter_fields
    max_action_parameters_bytes: int = DEFAULT_PLATFORM_SAFETY_LIMITS.max_action_parameters_bytes
    max_open_commitments: int = DEFAULT_PLATFORM_SAFETY_LIMITS.max_open_commitments
    max_commitment_bytes: int = DEFAULT_PLATFORM_SAFETY_LIMITS.max_commitment_bytes
    max_open_commitments_bytes: int = DEFAULT_PLATFORM_SAFETY_LIMITS.max_open_commitments_bytes
    max_memory_summary_bytes: int = DEFAULT_PLATFORM_SAFETY_LIMITS.max_memory_summary_bytes
    completion_requirements: Mapping[str, Any] = field(
        default_factory=_empty_completion_requirements
    )

    def __post_init__(self) -> None:
        if self.max_actions_per_turn < 0:
            raise ValueError("max_actions_per_turn cannot be negative")
        if self.max_timer_horizon <= timedelta(0):
            raise ValueError("max_timer_horizon must be positive")
        byte_and_count_limits = {
            "max_action_parameter_fields": self.max_action_parameter_fields,
            "max_action_parameters_bytes": self.max_action_parameters_bytes,
            "max_open_commitments": self.max_open_commitments,
            "max_commitment_bytes": self.max_commitment_bytes,
            "max_open_commitments_bytes": self.max_open_commitments_bytes,
            "max_memory_summary_bytes": self.max_memory_summary_bytes,
        }
        for name, value in byte_and_count_limits.items():
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
            platform_maximum = getattr(DEFAULT_PLATFORM_SAFETY_LIMITS, name)
            if value > platform_maximum:
                raise ValueError(f"{name} cannot exceed the platform maximum")
        if not self.human_wake_action_types.issubset(self.allowed_action_types):
            raise ValueError("human wake action types must be allowed action types")
        object.__setattr__(
            self,
            "completion_requirements",
            MappingProxyType(dict(self.completion_requirements)),
        )


def validate_decision(
    decision: AgentDecision,
    policy: DecisionPolicy,
    *,
    workflow_now: datetime,
    expected_event_ids: frozenset[UUID] | None = None,
    expected_review_command_ids: frozenset[UUID] | None = None,
    expected_action_attempt_ids: frozenset[UUID] | None = None,
    expected_timer_ids: frozenset[str] | None = None,
    conflicted_action_payload_hashes: frozenset[str] = frozenset(),
    current_authoritative_facts: Mapping[str, Any] | None = None,
    enforce_completion_requirements: bool = True,
) -> AgentDecision:
    """Return the unchanged decision if it fits policy; otherwise fail closed."""

    if workflow_now.tzinfo is None or workflow_now.utcoffset() is None:
        raise ValueError("workflow_now must be timezone-aware")

    if (
        expected_event_ids is not None
        and frozenset(decision.based_on_event_ids) != expected_event_ids
    ):
        raise DecisionRejected("decision must be based on exactly the events in this turn")
    if (
        expected_review_command_ids is not None
        and frozenset(decision.based_on_review_command_ids) != expected_review_command_ids
    ):
        raise DecisionRejected("decision must be based on exactly the review commands in this turn")
    if (
        expected_action_attempt_ids is not None
        and frozenset(decision.based_on_action_attempt_ids) != expected_action_attempt_ids
    ):
        raise DecisionRejected("decision must be based on exactly the action results in this turn")
    if (
        expected_timer_ids is not None
        and frozenset(decision.based_on_timer_ids) != expected_timer_ids
    ):
        raise DecisionRejected("decision must be based on exactly the timers in this turn")

    memory = decision.memory_update
    try:
        if memory.summary is not None:
            require_utf8_bytes(
                memory.summary,
                label="memory summary",
                max_bytes=policy.max_memory_summary_bytes,
            )
        require_item_count(
            memory.open_commitments,
            label="open commitments",
            max_items=policy.max_open_commitments,
        )
        for commitment in memory.open_commitments:
            require_utf8_bytes(
                commitment,
                label="open commitment",
                max_bytes=policy.max_commitment_bytes,
            )
        require_json_bytes(
            memory.open_commitments,
            label="open commitments",
            max_bytes=policy.max_open_commitments_bytes,
        )
        for action in decision.actions:
            require_item_count(
                action.parameters,
                label=f"action parameters for {action.logical_action_key}",
                max_items=policy.max_action_parameter_fields,
            )
            require_json_bytes(
                action.parameters,
                label=f"action parameters for {action.logical_action_key}",
                max_bytes=policy.max_action_parameters_bytes,
            )
    except ValueError as error:
        raise DecisionRejected(str(error)) from error

    if not set(memory.summary_source_event_ids).issubset(decision.based_on_event_ids):
        raise DecisionRejected("memory summary cites an event outside this turn")
    if not set(memory.summary_source_review_command_ids).issubset(
        decision.based_on_review_command_ids
    ):
        raise DecisionRejected("memory summary cites a review command outside this turn")
    if not set(memory.summary_source_action_attempt_ids).issubset(
        decision.based_on_action_attempt_ids
    ):
        raise DecisionRejected("memory summary cites an action result outside this turn")
    if not set(memory.summary_source_timer_ids).issubset(decision.based_on_timer_ids):
        raise DecisionRejected("memory summary cites a timer outside this turn")

    if len(decision.actions) > policy.max_actions_per_turn:
        raise DecisionRejected("decision exceeds the maximum actions per turn")
    if decision.status is DecisionStatus.COMPLETED and decision.actions:
        raise DecisionRejected("completed decision cannot propose unresolved actions")
    if (
        enforce_completion_requirements
        and decision.status is DecisionStatus.COMPLETED
        and policy.completion_requirements
    ):
        if current_authoritative_facts is None:
            raise DecisionRejected("authoritative facts are required to validate completion")
        unsatisfied = tuple(
            key
            for key, expected in sorted(policy.completion_requirements.items())
            if key not in current_authoritative_facts
            or current_authoritative_facts[key] != expected
        )
        if unsatisfied:
            raise DecisionRejected(
                "completion requirements are not satisfied: " + ", ".join(unsatisfied)
            )
    if (
        decision.status is DecisionStatus.ACTIVE
        and not decision.actions
        and not decision.wake_conditions
    ):
        raise DecisionRejected("active decision requires an action or wake condition")

    action_keys: set[str] = set()
    for action in decision.actions:
        if action.action_type not in policy.allowed_action_types:
            raise DecisionRejected(f"action type is not allowed: {action.action_type}")
        if action.logical_action_key in action_keys:
            raise DecisionRejected(f"duplicate logical action key: {action.logical_action_key}")
        if (
            action_payload_identity(action.action_type, action.parameters)
            in conflicted_action_payload_hashes
        ):
            raise DecisionRejected(
                "decision repeats an action payload that just returned a definitive conflict"
            )
        action_keys.add(action.logical_action_key)

    if any(isinstance(wake, HumanWakeCondition) for wake in decision.wake_conditions) and not any(
        action.action_type in policy.human_wake_action_types for action in decision.actions
    ):
        raise DecisionRejected(
            "human wake condition requires an action that requires human approval"
        )

    for wake in decision.wake_conditions:
        if isinstance(wake, EventWakeCondition):
            if wake.event_type not in policy.allowed_wake_event_types:
                raise DecisionRejected(f"wake event type is not allowed: {wake.event_type}")
        elif isinstance(wake, TimerWakeCondition):
            if wake.at <= workflow_now:
                raise DecisionRejected("timer wake condition must be in the future")
            if wake.at - workflow_now > policy.max_timer_horizon:
                raise DecisionRejected("timer wake condition exceeds the maximum horizon")

    return decision
