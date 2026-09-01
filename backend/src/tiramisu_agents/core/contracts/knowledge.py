"""Typed knowledge observations supplied by authoritative integration boundaries."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tiramisu_agents.core.limits import DEFAULT_PLATFORM_SAFETY_LIMITS, require_json_bytes


class FactKind(StrEnum):
    AUTHORITATIVE = "authoritative"
    CUSTOMER_CLAIM = "customer_claim"


class FactObservation(BaseModel):
    """One source-attributed fact or claim; models cannot produce this contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=200)
    kind: FactKind
    value: Any

    @field_validator("value")
    @classmethod
    def require_bounded_value(cls, value: Any) -> Any:
        require_json_bytes(
            value,
            label="fact value",
            max_bytes=DEFAULT_PLATFORM_SAFETY_LIMITS.max_fact_value_bytes,
        )
        return value
