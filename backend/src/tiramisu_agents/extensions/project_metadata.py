"""Business-readable metadata retained on a compiled client pack."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind


class FactDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    title: str
    description: str
    kinds: tuple[FactKind, ...]
    value_schema: dict[str, Any]
    operator_editable: bool = False


class CapabilityDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_type: str
    title: str
    description: str
    adapter_id: str
    default_permission: PermissionOutcome
    parameters_schema: dict[str, Any]
    produces_fact_keys: tuple[str, ...] = ()


class RouteDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["start", "wake"]
    event_type: str
    title: str
    description: str
    provides_fact_keys: tuple[str, ...] = ()


class ScenarioStepDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["event", "action", "wait", "fact", "complete"]
    description: str
    reference: str | None = None
    value: Any = None


class ScenarioDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    description: str
    steps: tuple[ScenarioStepDescription, ...]
    started_at: datetime


class JourneyDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: str
    title: str
    description: str
    status: str
    goals: tuple[str, ...]
    routes: tuple[RouteDescription, ...]
    capabilities: tuple[CapabilityDescription, ...]
    facts: tuple[FactDescription, ...]
    permissions: dict[str, PermissionOutcome]
    completion_requirements: dict[str, Any]
    scenarios: tuple[ScenarioDescription, ...]


class ProjectDescription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    version: str
    title: str
    description: str
    journeys: tuple[JourneyDescription, ...] = Field(min_length=1)
