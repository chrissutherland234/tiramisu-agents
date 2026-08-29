"""Client-pack registration metadata loaded before workers begin polling."""

from pydantic import BaseModel, ConfigDict, Field


class ExtensionManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    extension_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    extension_version: str = Field(min_length=1, max_length=100)
    tiramisu_compatibility: str = Field(min_length=1, max_length=100)
    process_definitions: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()
    activities: tuple[str, ...] = ()

    def registered_identifiers(self) -> frozenset[str]:
        return frozenset(
            (*self.process_definitions, *self.adapters, *self.policies, *self.activities)
        )
