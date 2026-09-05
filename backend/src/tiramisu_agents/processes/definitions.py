"""Configuration contracts compiled before workers begin polling."""

import json
import re
from datetime import time, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

    max_actions_per_turn: int = Field(default=3, ge=0, le=20)
    max_follow_ups_without_reply: int = Field(default=3, ge=0, le=100)
    minimum_follow_up_interval_hours: int = Field(default=24, ge=1, le=24 * 30)
    maximum_timer_horizon_days: int = Field(default=30, ge=1, le=365)
    max_outbound_messages_per_process: int = Field(default=50, ge=0, le=10_000)
    max_outbound_messages_per_window: int = Field(default=5, ge=0, le=1_000)
    outbound_message_window_hours: int = Field(default=24, ge=1, le=24 * 30)
    maximum_process_lifetime_days: int = Field(default=90, ge=1, le=3650)
    max_model_input_tokens_per_process: int = Field(default=1_000_000, ge=0, le=1_000_000_000)
    max_model_output_tokens_per_process: int = Field(default=250_000, ge=0, le=1_000_000_000)
    max_model_total_tokens_per_process: int = Field(default=1_000_000, ge=0, le=1_000_000_000)
    max_model_cost_micros_per_process: int = Field(default=20_000_000, ge=0, le=10**15)


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


class DailyQuietHours(BaseModel):
    """A recurring local-time interval in which outbound contact is forbidden."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str = Field(min_length=1, max_length=100)
    start_local: time
    end_local: time

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("quiet-hours timezone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "DailyQuietHours":
        if self.start_local.tzinfo is not None or self.end_local.tzinfo is not None:
            raise ValueError("quiet-hours times must be local clock times without UTC offsets")
        if self.start_local == self.end_local:
            raise ValueError("quiet-hours start and end must differ")
        return self


class CommunicationConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outbound_action_types: tuple[str, ...] = ()
    reply_event_types: tuple[str, ...] = ()
    opt_out_event_types: tuple[str, ...] = ()
    automated_response_event_types: tuple[str, ...] = ()
    quiet_hours: DailyQuietHours | None = None

    @field_validator(
        "outbound_action_types",
        "reply_event_types",
        "opt_out_event_types",
        "automated_response_event_types",
    )
    @classmethod
    def validate_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("communication action and event types must be unique")
        return values

    @model_validator(mode="after")
    def validate_event_roles(self) -> "CommunicationConfiguration":
        event_groups = (
            set(self.reply_event_types),
            set(self.opt_out_event_types),
            set(self.automated_response_event_types),
        )
        if any(
            left & right
            for index, left in enumerate(event_groups)
            for right in event_groups[index + 1 :]
        ):
            raise ValueError("communication event types must have one unambiguous role")
        if not self.outbound_action_types and any((*event_groups, self.quiet_hours is not None)):
            raise ValueError("communication controls require at least one outbound action type")
        return self


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
        communication_events = {
            *self.communications.opt_out_event_types,
            *self.communications.automated_response_event_types,
        }
        if not communication_events.issubset(self.allowed_wake_events):
            raise ValueError("communication control events must be allowed wake events")
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
        communication_rules = self._compile_communication_instructions()
        return (
            f"Process: {self.id} version {self.version}\n"
            f"Goals:\n{goals}\n"
            f"Allowed action types: {actions}\n"
            f"Action parameter guidance:\n{action_guidance}\n"
            f"Decision guidance:\n{decision_guidance}\n"
            f"Allowed event wake types: {wakes}\n"
            f"Business facts:\n{facts}\n"
            f"Completion requirements:\n{completion}\n"
            f"Communication safety:\n{communication_rules}\n"
            f"Model budget: at most {self.limits.max_model_total_tokens_per_process} "
            "tokens and deterministic cost enforcement per process; deterministic "
            "policy is authoritative.\n"
            f"Maximum process lifetime: {self.limits.maximum_process_lifetime_days} days "
            "from process creation.\n"
            f"Terminal states: {terminal_states}\n"
            "Propose only actions and wake conditions allowed above. "
            "Never claim that a proposed action has already executed."
        )

    def _compile_communication_instructions(self) -> str:
        communications = self.communications
        if not communications.outbound_action_types:
            return "- This journey has no customer-facing outbound actions."
        lines = [
            "- Outbound actions: " + ", ".join(communications.outbound_action_types),
            "- Never contact a customer after an opt-out or while an automated-response loop "
            "is active; deterministic policy is authoritative.",
            f"- At most {self.limits.max_follow_ups_without_reply} follow-ups without a human "
            "reply, separated by at least "
            f"{self.limits.minimum_follow_up_interval_hours} hours.",
            f"- At most {self.limits.max_outbound_messages_per_window} outbound messages in "
            f"{self.limits.outbound_message_window_hours} hours and "
            f"{self.limits.max_outbound_messages_per_process} over the process lifetime.",
        ]
        if communications.quiet_hours is not None:
            quiet = communications.quiet_hours
            lines.append(
                f"- Quiet hours are {quiet.start_local.isoformat(timespec='minutes')} to "
                f"{quiet.end_local.isoformat(timespec='minutes')} in {quiet.timezone}; "
                "wait until they end before proposing outbound contact."
            )
        return "\n".join(lines)
