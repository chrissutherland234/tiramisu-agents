"""Configuration contracts compiled before workers begin polling."""

import json
import re
from datetime import timedelta
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tiramisu_agents.core.action_policy import ConfiguredActionPolicy
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.processes import ProcessStatus
from tiramisu_agents.core.policy import DecisionPolicy

IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class DefinitionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class ProcessLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_actions_per_turn: int = Field(ge=0, le=20)
    max_follow_ups_without_reply: int = Field(ge=0, le=100)
    minimum_follow_up_interval_hours: int = Field(ge=1, le=24 * 30)
    maximum_timer_horizon_days: int = Field(ge=1, le=365)


class ReviewConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commands: tuple[str, ...] = ()


class ProcessDefinition(BaseModel):
    """Immutable source configuration for one process-definition version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: str = Field(min_length=1, max_length=64)
    status: DefinitionStatus
    trigger_events: tuple[str, ...] = ()
    goals: tuple[str, ...] = Field(min_length=1)
    terminal_states: tuple[ProcessStatus, ...] = Field(min_length=1)
    allowed_actions: tuple[str, ...] = ()
    action_permissions: dict[str, PermissionOutcome]
    allowed_wake_events: tuple[str, ...] = ()
    limits: ProcessLimits
    review: ReviewConfiguration = Field(default_factory=ReviewConfiguration)
    integrations: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("process definition ID must be a snake_case identifier")
        return value

    @field_validator("trigger_events", "allowed_wake_events")
    @classmethod
    def validate_event_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not EVENT_PATTERN.fullmatch(value) for value in values):
            raise ValueError("event types must use dotted lowercase identifiers")
        if len(values) != len(set(values)):
            raise ValueError("event types must be unique")
        return values

    @field_validator("allowed_actions")
    @classmethod
    def validate_action_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not IDENTIFIER_PATTERN.fullmatch(value) for value in values):
            raise ValueError("action types must be snake_case identifiers")
        if len(values) != len(set(values)):
            raise ValueError("action types must be unique")
        return values

    @field_validator("action_permissions")
    @classmethod
    def validate_permission_action_types(
        cls, values: dict[str, PermissionOutcome]
    ) -> dict[str, PermissionOutcome]:
        if any(not IDENTIFIER_PATTERN.fullmatch(value) for value in values):
            raise ValueError("action permission keys must be snake_case identifiers")
        return values

    @field_validator("goals")
    @classmethod
    def validate_goals(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("goals cannot be blank")
        return values

    @model_validator(mode="after")
    def require_explicit_action_permissions(self) -> "ProcessDefinition":
        if set(self.action_permissions) != set(self.allowed_actions):
            raise ValueError("every allowed action must have exactly one permission classification")
        return self

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    def decision_policy(self) -> DecisionPolicy:
        return DecisionPolicy(
            allowed_action_types=frozenset(self.allowed_actions),
            allowed_wake_event_types=frozenset(self.allowed_wake_events),
            max_actions_per_turn=self.limits.max_actions_per_turn,
            max_timer_horizon=timedelta(days=self.limits.maximum_timer_horizon_days),
        )

    def action_policy(self) -> ConfiguredActionPolicy:
        return ConfiguredActionPolicy(
            permissions=dict(self.action_permissions),
            version=self.fingerprint(),
        )

    def compile_instructions(self) -> str:
        goals = "\n".join(f"- {goal}" for goal in self.goals)
        actions = ", ".join(self.allowed_actions) or "none"
        wakes = ", ".join(self.allowed_wake_events) or "none"
        terminal_states = ", ".join(state.value for state in self.terminal_states)
        return (
            f"Process: {self.id} version {self.version}\n"
            f"Goals:\n{goals}\n"
            f"Allowed action types: {actions}\n"
            f"Allowed event wake types: {wakes}\n"
            f"Terminal states: {terminal_states}\n"
            "Propose only actions and wake conditions allowed above. "
            "Never claim that a proposed action has already executed."
        )
