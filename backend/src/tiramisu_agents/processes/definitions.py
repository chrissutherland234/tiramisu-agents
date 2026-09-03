"""Configuration contracts compiled before workers begin polling."""

import json
import re
from datetime import timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tiramisu_agents.core.action_policy import ConfiguredActionPolicy
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.core.contracts.processes import ProcessStatus
from tiramisu_agents.core.contracts.reviews import ReviewCommandType
from tiramisu_agents.core.limits import canonical_json_bytes
from tiramisu_agents.core.policy import DecisionPolicy
from tiramisu_agents.core.reserved_events import RESERVED_KERNEL_EVENT_TYPES

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

    commands: tuple[ReviewCommandType, ...] = ()

    @field_validator("commands")
    @classmethod
    def validate_supported_commands(
        cls, values: tuple[ReviewCommandType, ...]
    ) -> tuple[ReviewCommandType, ...]:
        supported = {
            ReviewCommandType.APPROVE,
            ReviewCommandType.REJECT,
            ReviewCommandType.REQUEST_REVISION,
            ReviewCommandType.COMMENT,
        }
        if not set(values).issubset(supported):
            raise ValueError("process definition advertises an unsupported review command")
        if len(values) != len(set(values)):
            raise ValueError("review commands must be unique")
        return values


class CommunicationConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outbound_action_types: tuple[str, ...] = ()
    reply_event_types: tuple[str, ...] = ()


class FactDefinition(BaseModel):
    """Business-readable schema for one fact available to a journey."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2_000)
    kinds: tuple[FactKind, ...] = Field(min_length=1)
    value_schema: dict[str, Any]
    operator_editable: bool = False

    @field_validator("kinds")
    @classmethod
    def validate_kinds(cls, values: tuple[FactKind, ...]) -> tuple[FactKind, ...]:
        if len(values) != len(set(values)):
            raise ValueError("fact kinds must be unique")
        return values


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
    action_guidance: dict[str, str] = Field(default_factory=dict)
    decision_guidance: tuple[str, ...] = ()
    allowed_wake_events: tuple[str, ...] = ()
    limits: ProcessLimits
    review: ReviewConfiguration = Field(default_factory=ReviewConfiguration)
    communications: CommunicationConfiguration = Field(default_factory=CommunicationConfiguration)
    integrations: dict[str, str] = Field(default_factory=dict)
    facts: tuple[FactDefinition, ...] = ()
    completion_requirements: dict[str, Any] = Field(default_factory=dict)

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
        reserved = set(values) & RESERVED_KERNEL_EVENT_TYPES
        if reserved:
            raise ValueError(
                f"event types are reserved for kernel use: {', '.join(sorted(reserved))}"
            )
        return values

    @field_validator("allowed_actions")
    @classmethod
    def validate_action_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not IDENTIFIER_PATTERN.fullmatch(value) for value in values):
            raise ValueError("action types must be snake_case identifiers")
        if len(values) != len(set(values)):
            raise ValueError("action types must be unique")
        return values

    @field_validator("action_guidance")
    @classmethod
    def validate_action_guidance(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not IDENTIFIER_PATTERN.fullmatch(action_type) for action_type in values):
            raise ValueError("action guidance keys must be snake_case action types")
        if any(not guidance.strip() for guidance in values.values()):
            raise ValueError("action guidance cannot be blank")
        return values

    @field_validator("decision_guidance")
    @classmethod
    def validate_decision_guidance(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not guidance.strip() for guidance in values):
            raise ValueError("decision guidance cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("decision guidance must be unique")
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

    @field_validator("completion_requirements")
    @classmethod
    def validate_completion_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        for key, value in values.items():
            if not re.fullmatch(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", key):
                raise ValueError("completion requirement keys must be dotted lowercase facts")
            canonical_json_bytes(value, label=f"completion requirement {key}")
        return values

    @field_validator("terminal_states")
    @classmethod
    def validate_terminal_states(
        cls, values: tuple[ProcessStatus, ...]
    ) -> tuple[ProcessStatus, ...]:
        terminal = {
            ProcessStatus.COMPLETED,
            ProcessStatus.CANCELLED,
            ProcessStatus.FAILED,
        }
        if not set(values).issubset(terminal):
            raise ValueError("terminal_states can only contain terminal process statuses")
        if ProcessStatus.COMPLETED not in values:
            raise ValueError("terminal_states must include completed")
        return values

    @model_validator(mode="after")
    def require_explicit_action_permissions(self) -> "ProcessDefinition":
        if set(self.action_permissions) != set(self.allowed_actions):
            raise ValueError("every allowed action must have exactly one permission classification")
        if not set(self.action_guidance).issubset(self.allowed_actions):
            raise ValueError("action guidance can only describe allowed actions")
        if not set(self.communications.outbound_action_types).issubset(self.allowed_actions):
            raise ValueError("communication actions must be allowed actions")
        if not set(self.communications.reply_event_types).issubset(self.allowed_wake_events):
            raise ValueError("communication reply events must be allowed wake events")
        fact_by_key = {fact.key: fact for fact in self.facts}
        if len(fact_by_key) != len(self.facts):
            raise ValueError("fact definitions must have unique keys")
        if not set(self.completion_requirements).issubset(fact_by_key):
            raise ValueError("completion requirements must reference declared facts")
        if any(
            FactKind.AUTHORITATIVE not in fact_by_key[key].kinds
            for key in self.completion_requirements
        ):
            raise ValueError("completion requirements must use authoritative facts")
        return self

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    def decision_policy(self) -> DecisionPolicy:
        return DecisionPolicy(
            allowed_action_types=frozenset(self.allowed_actions),
            allowed_wake_event_types=frozenset(self.allowed_wake_events),
            human_wake_action_types=frozenset(
                action_type
                for action_type, permission in self.action_permissions.items()
                if permission is PermissionOutcome.REQUIRE_APPROVAL
            ),
            max_actions_per_turn=self.limits.max_actions_per_turn,
            max_timer_horizon=timedelta(days=self.limits.maximum_timer_horizon_days),
            completion_requirements=dict(self.completion_requirements),
        )

    def action_policy(self) -> ConfiguredActionPolicy:
        return ConfiguredActionPolicy(
            permissions=dict(self.action_permissions),
            version=self.fingerprint(),
        )

    def compile_instructions(self) -> str:
        goals = "\n".join(f"- {goal}" for goal in self.goals)
        actions = ", ".join(self.allowed_actions) or "none"
        action_guidance = (
            "\n".join(
                f"- {action}: {self.action_guidance[action]}"
                for action in self.allowed_actions
                if action in self.action_guidance
            )
            or "- No additional parameter guidance was declared."
        )
        decision_guidance = "\n".join(f"- {guidance}" for guidance in self.decision_guidance)
        if not decision_guidance:
            decision_guidance = "- No additional decision guidance was declared."
        wakes = ", ".join(self.allowed_wake_events) or "none"
        terminal_states = ", ".join(state.value for state in self.terminal_states)
        facts = (
            "\n".join(
                f"- {fact.key} ({fact.title}; {', '.join(kind.value for kind in fact.kinds)}): "
                f"{fact.description}"
                for fact in self.facts
            )
            or "- No business facts were declared."
        )
        completion = (
            "\n".join(
                f"- {key} must equal {json.dumps(value, sort_keys=True)}"
                for key, value in sorted(self.completion_requirements.items())
            )
            or "- No additional fact requirements were declared."
        )
        return (
            f"Process: {self.id} version {self.version}\n"
            f"Goals:\n{goals}\n"
            f"Allowed action types: {actions}\n"
            f"Action parameter guidance:\n{action_guidance}\n"
            f"Decision guidance:\n{decision_guidance}\n"
            f"Allowed event wake types: {wakes}\n"
            f"Business facts:\n{facts}\n"
            f"Completion requirements:\n{completion}\n"
            f"Terminal states: {terminal_states}\n"
            "Propose only actions and wake conditions allowed above. "
            "Never claim that a proposed action has already executed."
        )
