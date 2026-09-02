"""Contracts for the durable action permission boundary."""

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.limits import require_action_parameters


class PermissionOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ActionRequestStatus(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"


class ActionAttemptStatus(StrEnum):
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"


class ActionResolution(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ActionConflict(BaseModel):
    """A definitive, provider-declared resource or state conflict.

    The platform preserves this as durable context but does not assign domain
    meaning to the code or decide whether an alternative can be proposed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    message: str = Field(min_length=1, max_length=2_000)
    details: dict[str, Any] = Field(default_factory=dict)
    facts: tuple[FactObservation, ...] = ()

    @field_validator("details")
    @classmethod
    def _validate_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        require_action_parameters(value, label="action conflict details")
        return value

    @model_validator(mode="after")
    def _require_authoritative_facts(self) -> "ActionConflict":
        if any(fact.kind is not FactKind.AUTHORITATIVE for fact in self.facts):
            raise ValueError("action conflict facts must be authoritative")
        return self


class OperatorActionResolution(BaseModel):
    """Evidence-backed command for resolving a genuinely unknown provider outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    process_instance_id: UUID
    action_attempt_id: UUID
    actor_id: UUID
    resolution: ActionResolution
    evidence: str = Field(min_length=1, max_length=10_000)
    provider_reference: str | None = Field(default=None, max_length=500)
    result: dict[str, Any] | None = None


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
