"""Stable semantic identities for provider action payloads."""

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from uuid import UUID


def action_payload_identity(action_type: str, parameters: Mapping[str, Any]) -> str:
    """Hash provider-visible action content, excluding model explanation and local IDs."""

    payload = {"action_type": action_type, "parameters": parameters}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


def execution_idempotency_key(
    tenant_id: UUID,
    process_instance_id: UUID,
    action_request_id: UUID,
    revision: int,
    payload_hash: str,
) -> str:
    """Derive the stable provider execution key from one exact action revision."""

    identity = f"{tenant_id}:{process_instance_id}:{action_request_id}:{revision}:{payload_hash}"
    return sha256(identity.encode()).hexdigest()
