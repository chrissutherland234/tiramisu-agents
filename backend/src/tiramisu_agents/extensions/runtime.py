"""Deterministic identity for one immutable deployed client-pack release."""

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256

from tiramisu_agents import __version__
from tiramisu_agents.processes.compatibility import DeploymentCompatibilityError

_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def normalize_deployment_id(value: str) -> str:
    normalized = value.strip().lower()
    if _DEPLOYMENT_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "deployment ID must contain 1 to 63 lowercase letters, digits, or hyphens, "
            "starting with a letter"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class DeploymentRelease:
    """One immutable API/worker composition and its dedicated task queue."""

    deployment_id: str
    build_id: str
    client_pack_fingerprint: str
    model_id: str
    release_fingerprint: str = field(init=False)
    temporal_task_queue: str = field(init=False)

    def __post_init__(self) -> None:
        deployment_id = normalize_deployment_id(self.deployment_id)
        if deployment_id == "unassigned":
            raise ValueError("unassigned is reserved for legacy or unconfigured tenants")
        build_id = self.build_id.strip()
        model_id = self.model_id.strip()
        if not build_id or len(build_id) > 200:
            raise ValueError("deployment build ID must contain 1 to 200 characters")
        if not model_id or len(model_id) > 200:
            raise ValueError("deployment model ID must contain 1 to 200 characters")
        if _SHA256_PATTERN.fullmatch(self.client_pack_fingerprint) is None:
            raise ValueError("client-pack fingerprint must be lowercase SHA-256 hex")
        object.__setattr__(self, "deployment_id", deployment_id)
        object.__setattr__(self, "build_id", build_id)
        object.__setattr__(self, "model_id", model_id)
        canonical = json.dumps(
            {
                "schema_version": 1,
                "deployment_id": deployment_id,
                "build_id": build_id,
                "client_pack_fingerprint": self.client_pack_fingerprint,
                "model_id": model_id,
                "tiramisu_version": __version__,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = sha256(canonical.encode()).hexdigest()
        object.__setattr__(self, "release_fingerprint", fingerprint)
        object.__setattr__(
            self,
            "temporal_task_queue",
            f"tiramisu.{deployment_id}.{fingerprint}",
        )

    def require_client_pack(self, fingerprint: str) -> None:
        if fingerprint != self.client_pack_fingerprint:
            raise ValueError("deployment release and client pack fingerprints disagree")

    def require_process(
        self,
        *,
        deployment_id: str,
        deployment_release_fingerprint: str,
        temporal_task_queue: str,
    ) -> None:
        mismatches: list[str] = []
        if deployment_id != self.deployment_id:
            mismatches.append("deployment")
        if deployment_release_fingerprint != self.release_fingerprint:
            mismatches.append("deployment release")
        if temporal_task_queue != self.temporal_task_queue:
            mismatches.append("Temporal task queue")
        if mismatches:
            raise DeploymentCompatibilityError(
                "process deployment pins do not match the running release: " + ", ".join(mismatches)
            )
