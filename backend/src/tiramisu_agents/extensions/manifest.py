"""Client-pack registration metadata loaded before workers begin polling."""

import json
from hashlib import sha256
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtensionManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    extension_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    extension_version: str = Field(min_length=1, max_length=100)
    tiramisu_compatibility: str = Field(min_length=1, max_length=100)
    process_definitions: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()
    activities: tuple[str, ...] = ()

    @field_validator("process_definitions", "adapters", "policies", "activities")
    @classmethod
    def require_unique_nonblank_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("extension identifiers cannot be blank")
        if len(values) != len(set(values)):
            raise ValueError("extension identifiers must be unique within a category")
        return values

    @model_validator(mode="after")
    def require_globally_unique_identifiers(self) -> Self:
        identifiers = (
            *self.process_definitions,
            *self.adapters,
            *self.policies,
            *self.activities,
        )
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("extension identifiers must be unique across categories")
        return self

    def registered_identifiers(self) -> frozenset[str]:
        return frozenset(
            (*self.process_definitions, *self.adapters, *self.policies, *self.activities)
        )

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()
