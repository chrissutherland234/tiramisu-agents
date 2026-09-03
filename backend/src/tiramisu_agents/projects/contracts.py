"""Opinionated, author-facing contracts for Tiramisu client projects."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, TypeAdapter

from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.core.contracts.processes import AgentTurnInput, ProcessStatus
from tiramisu_agents.core.contracts.reviews import ReviewCommandType
from tiramisu_agents.core.limits import canonical_json_bytes
from tiramisu_agents.processes.definitions import DefinitionStatus, ProcessLimits

if TYPE_CHECKING:
    from tiramisu_agents.extensions import ClientPack

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class ProjectConfigurationError(ValueError):
    """Raised when an author-facing project cannot compile safely."""


DecisionTransformer = Callable[[AgentDecision, AgentTurnInput], AgentDecision]


def _require_identifier(value: str, *, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ProjectConfigurationError(f"{label} must be a snake_case identifier")


def _require_nonblank(value: str, *, label: str) -> None:
    if not value.strip():
        raise ProjectConfigurationError(f"{label} cannot be blank")


def _require_parameter_model(value: object, *, action_type: str) -> type[BaseModel]:
    if not isinstance(value, type) or not issubclass(value, BaseModel):
        raise ProjectConfigurationError(
            f"capability {action_type} parameters_model must be a Pydantic model"
        )
    return value


@dataclass(frozen=True, slots=True)
class FactRequirement:
    fact_key: str
    expected_value: Any

    def __post_init__(self) -> None:
        canonical_json_bytes(self.expected_value, label=f"completion value for {self.fact_key}")


@dataclass(frozen=True, slots=True)
class Fact:
    """One named piece of business knowledge understood by a project."""

    key: str
    title: str
    description: str
    value_type: Any = Any
    kinds: tuple[FactKind, ...] = (FactKind.AUTHORITATIVE,)
    operator_editable: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", self.key):
            raise ProjectConfigurationError("fact key must be a dotted lowercase identifier")
        _require_nonblank(self.title, label=f"fact {self.key} title")
        _require_nonblank(self.description, label=f"fact {self.key} description")
        if not self.kinds or len(self.kinds) != len(set(self.kinds)):
            raise ProjectConfigurationError(f"fact {self.key} kinds must be unique and nonempty")
        if self.operator_editable and FactKind.AUTHORITATIVE not in self.kinds:
            raise ProjectConfigurationError(
                f"operator-editable fact {self.key} must permit authoritative observations"
            )
        TypeAdapter(self.value_type).json_schema()

    @property
    def value_schema(self) -> dict[str, Any]:
        return TypeAdapter(self.value_type).json_schema()

    def equals(self, value: Any) -> FactRequirement:
        validated = TypeAdapter(self.value_type).validate_python(value)
        encoded = TypeAdapter(self.value_type).dump_python(validated, mode="json")
        return FactRequirement(fact_key=self.key, expected_value=encoded)


@dataclass(frozen=True, slots=True)
class Capability:
    """A typed business operation backed by one provider-neutral adapter."""

    action_type: str
    title: str
    description: str
    parameters_model: type[BaseModel]
    adapter: Any
    guidance: str
    default_permission: PermissionOutcome = PermissionOutcome.REQUIRE_APPROVAL
    produces: tuple[Fact, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.action_type, label="capability action type")
        _require_nonblank(self.title, label=f"capability {self.action_type} title")
        _require_nonblank(self.description, label=f"capability {self.action_type} description")
        _require_nonblank(self.guidance, label=f"capability {self.action_type} guidance")
        parameters_model = _require_parameter_model(
            self.parameters_model, action_type=self.action_type
        )
        schema = parameters_model.model_json_schema()
        if schema.get("additionalProperties") is not False:
            raise ProjectConfigurationError(
                f"capability {self.action_type} parameters must forbid unknown fields"
            )
        adapter_id = getattr(self.adapter, "id", None)
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            raise ProjectConfigurationError(
                f"capability {self.action_type} adapter must expose a nonblank id"
            )
        if not isinstance(getattr(self.adapter, "guarantees_idempotency", None), bool):
            raise ProjectConfigurationError(
                f"capability {self.action_type} adapter must declare idempotency behavior"
            )
        if not callable(getattr(self.adapter, "execute", None)) or not callable(
            getattr(self.adapter, "lookup", None)
        ):
            raise ProjectConfigurationError(
                f"capability {self.action_type} adapter must implement execute and lookup"
            )
        if len({fact.key for fact in self.produces}) != len(self.produces):
            raise ProjectConfigurationError(
                f"capability {self.action_type} declares a fact more than once"
            )


RouteKind = Literal["start", "wake"]


@dataclass(frozen=True, slots=True)
class Route:
    """Route one canonical business event to a journey start or wake."""

    kind: RouteKind
    event_type: str
    journey_id: str
    title: str
    description: str
    provides: tuple[Fact, ...] = ()

    def __post_init__(self) -> None:
        if not _EVENT_TYPE.fullmatch(self.event_type):
            raise ProjectConfigurationError(
                "route event type must be a dotted lowercase identifier"
            )
        _require_identifier(self.journey_id, label="route journey ID")
        _require_nonblank(self.title, label=f"route {self.event_type} title")
        _require_nonblank(self.description, label=f"route {self.event_type} description")
        if len({fact.key for fact in self.provides}) != len(self.provides):
            raise ProjectConfigurationError(f"route {self.event_type} declares a fact twice")

    @classmethod
    def start(
        cls,
        event_type: str,
        *,
        journey: str,
        title: str,
        description: str,
        provides: tuple[Fact, ...] = (),
    ) -> "Route":
        return cls("start", event_type, journey, title, description, provides)

    @classmethod
    def wake(
        cls,
        event_type: str,
        *,
        journey: str,
        title: str,
        description: str,
        provides: tuple[Fact, ...] = (),
    ) -> "Route":
        return cls("wake", event_type, journey, title, description, provides)


ScenarioStepKind = Literal["event", "action", "fact", "complete"]


@dataclass(frozen=True, slots=True)
class ScenarioStep:
    """One business-readable expectation in an example journey."""

    kind: ScenarioStepKind
    description: str
    reference: str | None = None
    value: Any = None

    def __post_init__(self) -> None:
        _require_nonblank(self.description, label="scenario step description")
        if self.kind != "complete" and not self.reference:
            raise ProjectConfigurationError(f"scenario {self.kind} step requires a reference")
        canonical_json_bytes(self.value, label="scenario step value")

    @classmethod
    def event(cls, event_type: str, description: str) -> "ScenarioStep":
        return cls("event", description, event_type)

    @classmethod
    def action(cls, action_type: str, description: str) -> "ScenarioStep":
        return cls("action", description, action_type)

    @classmethod
    def fact(cls, fact: Fact, value: Any, description: str) -> "ScenarioStep":
        return cls("fact", description, fact.key, fact.equals(value).expected_value)

    @classmethod
    def complete(cls, description: str) -> "ScenarioStep":
        return cls("complete", description)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    journey_id: str
    title: str
    description: str
    steps: tuple[ScenarioStep, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.id, label="scenario ID")
        _require_identifier(self.journey_id, label="scenario journey ID")
        _require_nonblank(self.title, label=f"scenario {self.id} title")
        _require_nonblank(self.description, label=f"scenario {self.id} description")
        if not self.steps:
            raise ProjectConfigurationError(f"scenario {self.id} requires at least one step")


def _default_limits() -> ProcessLimits:
    return ProcessLimits(
        max_actions_per_turn=3,
        max_follow_ups_without_reply=3,
        minimum_follow_up_interval_hours=24,
        maximum_timer_horizon_days=30,
    )


@dataclass(frozen=True, slots=True)
class Journey:
    """The deterministic rails and business goal for one long-running agent."""

    id: str
    version: str
    title: str
    description: str
    goals: tuple[str, ...]
    capabilities: tuple[str, ...]
    complete_when: tuple[FactRequirement, ...]
    status: DefinitionStatus = DefinitionStatus.PUBLISHED
    permission_overrides: Mapping[str, PermissionOutcome] = field(
        default_factory=lambda: MappingProxyType({})
    )
    action_guidance: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    decision_guidance: tuple[str, ...] = ()
    terminal_states: tuple[ProcessStatus, ...] = (
        ProcessStatus.COMPLETED,
        ProcessStatus.CANCELLED,
    )
    limits: ProcessLimits = field(default_factory=_default_limits)
    review_commands: tuple[ReviewCommandType, ...] = (
        ReviewCommandType.APPROVE,
        ReviewCommandType.REJECT,
        ReviewCommandType.REQUEST_REVISION,
        ReviewCommandType.COMMENT,
    )
    outbound_action_types: tuple[str, ...] = ()
    reply_event_types: tuple[str, ...] = ()
    decision_transformer: DecisionTransformer | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _require_identifier(self.id, label="journey ID")
        _require_nonblank(self.version, label=f"journey {self.id} version")
        _require_nonblank(self.title, label=f"journey {self.id} title")
        _require_nonblank(self.description, label=f"journey {self.id} description")
        if not self.goals or any(not goal.strip() for goal in self.goals):
            raise ProjectConfigurationError(f"journey {self.id} requires nonblank goals")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ProjectConfigurationError(f"journey {self.id} capabilities must be unique")
        if not self.complete_when:
            raise ProjectConfigurationError(
                f"journey {self.id} must declare authoritative completion requirements"
            )
        if len({item.fact_key for item in self.complete_when}) != len(self.complete_when):
            raise ProjectConfigurationError(f"journey {self.id} completion facts must be unique")
        for label, values in (
            ("outbound action types", self.outbound_action_types),
            ("reply event types", self.reply_event_types),
        ):
            if len(values) != len(set(values)):
                raise ProjectConfigurationError(f"journey {self.id} {label} must be unique")
        object.__setattr__(
            self,
            "permission_overrides",
            MappingProxyType(dict(self.permission_overrides)),
        )
        object.__setattr__(self, "action_guidance", MappingProxyType(dict(self.action_guidance)))


@dataclass(frozen=True, slots=True)
class Project:
    """One conventional client implementation compiled into a safe ClientPack."""

    id: str
    version: str
    title: str
    description: str
    journeys: tuple[Journey, ...]
    routes: tuple[Route, ...]
    capabilities: tuple[Capability, ...]
    facts: tuple[Fact, ...] = ()
    scenarios: tuple[Scenario, ...] = ()
    tiramisu_compatibility: str = ">=0.1,<0.2"

    def __post_init__(self) -> None:
        _require_identifier(self.id, label="project ID")
        _require_nonblank(self.version, label="project version")
        _require_nonblank(self.title, label="project title")
        _require_nonblank(self.description, label="project description")
        if not self.journeys:
            raise ProjectConfigurationError("a project requires at least one journey")

    def compile(self) -> "ClientPack":
        from tiramisu_agents.projects.compiler import compile_project

        return compile_project(self)
