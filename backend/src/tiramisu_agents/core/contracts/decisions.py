"""Typed output produced by a bounded agent turn."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    WAITING = "waiting"
    COMPLETED = "completed"
    ESCALATED = "escalated"


class ActionProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_request_id: UUID = Field(default_factory=uuid4)
    logical_action_key: str = Field(min_length=1, max_length=200)
    action_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=1000)


class EventWakeCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["event"] = "event"
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class TimerWakeCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["timer"] = "timer"
    at: datetime

    @field_validator("at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timer must be timezone-aware")
        return value


class HumanWakeCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["human"] = "human"
    interaction: Literal["approval", "review", "operator"]


WakeCondition = Annotated[
    EventWakeCondition | TimerWakeCondition | HumanWakeCondition,
    Field(discriminator="type"),
]


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str | None = Field(default=None, max_length=4000)
    summary_source_event_ids: tuple[UUID, ...] = ()
    summary_source_review_command_ids: tuple[UUID, ...] = ()
    summary_source_action_attempt_ids: tuple[UUID, ...] = ()
    summary_source_timer_ids: tuple[str, ...] = ()
    open_commitments: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_bounded_memory_provenance(self) -> "MemoryUpdate":
        sources = (
            self.summary_source_event_ids,
            self.summary_source_review_command_ids,
            self.summary_source_action_attempt_ids,
            self.summary_source_timer_ids,
        )
        if self.summary is None and any(sources):
            raise ValueError("memory summary sources require a summary")
        if self.summary is not None and not any(sources):
            raise ValueError("a memory summary requires source provenance")
        for values in sources:
            if len(values) != len(set(values)):
                raise ValueError("memory summary source IDs must be unique")
        if any(not commitment.strip() for commitment in self.open_commitments):
            raise ValueError("open commitments cannot be blank")
        if len(self.open_commitments) != len(set(self.open_commitments)):
            raise ValueError("open commitments must be unique")
        return self


class AgentDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID = Field(default_factory=uuid4)
    based_on_event_ids: tuple[UUID, ...]
    based_on_review_command_ids: tuple[UUID, ...] = ()
    based_on_action_attempt_ids: tuple[UUID, ...] = ()
    based_on_timer_ids: tuple[str, ...] = ()
    status: DecisionStatus
    actions: tuple[ActionProposal, ...] = ()
    wake_conditions: tuple[WakeCondition, ...] = ()
    memory_update: MemoryUpdate = Field(default_factory=MemoryUpdate)

    @model_validator(mode="after")
    def waiting_requires_a_wake_condition(self) -> "AgentDecision":
        if self.status is DecisionStatus.WAITING and not self.wake_conditions:
            raise ValueError("a waiting decision requires at least one wake condition")
        return self
