"""Deterministic validation applied after an agent proposes a decision."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from tiramisu_agents.core.contracts.decisions import (
    AgentDecision,
    EventWakeCondition,
    TimerWakeCondition,
)


class DecisionRejected(ValueError):
    """Raised when a proposal falls outside deterministic process policy."""


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    allowed_action_types: frozenset[str]
    allowed_wake_event_types: frozenset[str]
    max_actions_per_turn: int = 5
    max_timer_horizon: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        if self.max_actions_per_turn < 0:
            raise ValueError("max_actions_per_turn cannot be negative")
        if self.max_timer_horizon <= timedelta(0):
            raise ValueError("max_timer_horizon must be positive")


def validate_decision(
    decision: AgentDecision,
    policy: DecisionPolicy,
    *,
    workflow_now: datetime,
    expected_event_ids: frozenset[UUID] | None = None,
    expected_review_command_ids: frozenset[UUID] | None = None,
    expected_action_attempt_ids: frozenset[UUID] | None = None,
    expected_timer_ids: frozenset[str] | None = None,
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

    action_keys: set[str] = set()
    for action in decision.actions:
        if action.action_type not in policy.allowed_action_types:
            raise DecisionRejected(f"action type is not allowed: {action.action_type}")
        if action.logical_action_key in action_keys:
            raise DecisionRejected(f"duplicate logical action key: {action.logical_action_key}")
        action_keys.add(action.logical_action_key)

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
