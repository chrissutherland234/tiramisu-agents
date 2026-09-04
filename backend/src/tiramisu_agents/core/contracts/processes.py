"""Process state exposed to a bounded agent turn."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tiramisu_agents.core.contracts.actions import ActionAttemptStatus, ActionConflict
from tiramisu_agents.core.contracts.decisions import WakeCondition
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactObservation


class ReviewTurnContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: UUID
    command_type: str
    review_thread_id: UUID
    action_request_id: UUID
    proposal_revision: int = Field(ge=1)
    actor_id: UUID
    message: str | None = None
    action_type: str
    proposal_parameters: dict[str, Any]
    proposal_payload_hash: str
    proposal_rationale: str


class ActionResultContext(BaseModel):
    """Authoritative provider outcome exposed to one bounded follow-up turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: UUID
    action_request_id: UUID
    revision: int = Field(ge=1)
    action_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    parameters: dict[str, Any]
    status: ActionAttemptStatus
    adapter_id: str = Field(min_length=1, max_length=150)
    idempotency_key: str = Field(min_length=64, max_length=64)
    provider_reference: str | None = None
    result: dict[str, Any] | None = None
    facts: tuple[FactObservation, ...] = ()
    error: str | None = None
    conflict: ActionConflict | None = None
    operator_resolution_id: UUID | None = None
    operator_actor_id: UUID | None = None
    operator_evidence: str | None = None


class ProcessStatus(StrEnum):
    ACTIVE = "active"
    WAITING = "waiting"
    REVIEW = "review"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ProcessSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    process_instance_id: UUID
    process_type: str = Field(min_length=1, max_length=100)
    process_definition_version: str = Field(min_length=1, max_length=100)
    status: ProcessStatus
    authoritative_facts: dict[str, Any] = Field(default_factory=dict)
    customer_claims: dict[str, Any] = Field(default_factory=dict)
    fact_provenance: dict[str, dict[str, Any]] = Field(default_factory=dict)
    memory_summary: str | None = None
    memory_summary_source_event_ids: tuple[UUID, ...] = ()
    memory_summary_source_review_command_ids: tuple[UUID, ...] = ()
    memory_summary_source_action_attempt_ids: tuple[UUID, ...] = ()
    memory_summary_source_timer_ids: tuple[str, ...] = ()
    open_commitments: tuple[str, ...] = ()
    current_wake_conditions: tuple[WakeCondition, ...] = ()
    state_version: int = Field(default=0, ge=0)


class AgentTurnInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    workflow_now: datetime | None = None
    process: ProcessSnapshot
    events: tuple[CanonicalEvent, ...]
    reviews: tuple[ReviewTurnContext, ...] = ()
    action_results: tuple[ActionResultContext, ...] = ()
    timer_ids: tuple[str, ...] = ()
    instructions: str = Field(min_length=1)

    @field_validator("workflow_now")
    @classmethod
    def require_workflow_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("agent workflow time must be timezone-aware")
        return value
