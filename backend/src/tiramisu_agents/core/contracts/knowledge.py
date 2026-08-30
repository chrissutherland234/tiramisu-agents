"""Typed knowledge observations supplied by authoritative integration boundaries."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FactKind(StrEnum):
    AUTHORITATIVE = "authoritative"
    CUSTOMER_CLAIM = "customer_claim"


class FactObservation(BaseModel):
    """One source-attributed fact or claim; models cannot produce this contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=200)
    kind: FactKind
    value: Any
