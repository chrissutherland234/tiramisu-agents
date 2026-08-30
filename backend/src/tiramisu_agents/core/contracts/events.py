"""Canonical event contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tiramisu_agents.core.contracts.knowledge import FactObservation


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ExternalReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=100)
    resource_type: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=500)


class CanonicalEvent(BaseModel):
    """A versioned event accepted by the process mailbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    process_instance_id: UUID | None = None
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    source: str = Field(min_length=1, max_length=100)
    source_event_id: str = Field(min_length=1, max_length=500)
    occurred_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = Field(default=1, ge=1)
    sensitivity: Sensitivity = Sensitivity.CONFIDENTIAL
    external_references: tuple[ExternalReference, ...] = ()
    facts: tuple[FactObservation, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamps must be timezone-aware")
        return value
