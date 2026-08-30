"""Process state exposed to a bounded agent turn."""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tiramisu_agents.core.contracts.events import CanonicalEvent


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
    memory_summary: str | None = None
    open_commitments: tuple[str, ...] = ()


class AgentTurnInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: UUID
    process: ProcessSnapshot
    events: tuple[CanonicalEvent, ...]
    reviews: tuple[ReviewTurnContext, ...] = ()
    instructions: str = Field(min_length=1)
