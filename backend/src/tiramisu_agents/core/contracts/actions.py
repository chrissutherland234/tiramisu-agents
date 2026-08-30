"""Contracts for the durable action permission boundary."""

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


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
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"


class ActionAttemptStatus(StrEnum):
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"


class ActionResolution(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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
