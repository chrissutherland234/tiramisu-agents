"""Fail-closed compatibility checks for long-lived process instances."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class DeploymentCompatibilityError(ValueError):
    """Raised when persisted process pins do not match the running client pack."""


@dataclass(frozen=True, slots=True)
class DeploymentCompatibility:
    """Immutable fingerprints accepted by one API/worker deployment."""

    client_pack_fingerprint: str
    extension_manifest_hash: str
    definition_fingerprints: Mapping[tuple[str, str], str]

    def __post_init__(self) -> None:
        fingerprints = MappingProxyType(dict(self.definition_fingerprints))
        object.__setattr__(self, "definition_fingerprints", fingerprints)
        values = (
            self.client_pack_fingerprint,
            self.extension_manifest_hash,
            *fingerprints.values(),
        )
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("deployment compatibility fingerprints must be lowercase SHA-256 hex")

    def require_process(
        self,
        *,
        process_type: str,
        definition_version: str,
        client_pack_fingerprint: str,
        extension_manifest_hash: str,
        process_definition_fingerprint: str,
    ) -> None:
        """Reject a process unless every persisted compatibility pin is exact."""

        identity = (process_type, definition_version)
        expected_definition = self.definition_fingerprints.get(identity)
        if expected_definition is None:
            raise DeploymentCompatibilityError(
                "process definition is not present in the deployed client pack"
            )

        mismatches: list[str] = []
        if client_pack_fingerprint != self.client_pack_fingerprint:
            mismatches.append("client pack")
        if extension_manifest_hash != self.extension_manifest_hash:
            mismatches.append("extension manifest")
        if process_definition_fingerprint != expected_definition:
            mismatches.append("process definition")
        if mismatches:
            raise DeploymentCompatibilityError(
                "process compatibility pins do not match the deployed composition: "
                + ", ".join(mismatches)
            )
