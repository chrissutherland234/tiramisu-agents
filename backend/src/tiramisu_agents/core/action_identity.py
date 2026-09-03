"""Stable semantic identities for provider action payloads."""

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any


def action_payload_identity(action_type: str, parameters: Mapping[str, Any]) -> str:
    """Hash provider-visible action content, excluding model explanation and local IDs."""

    payload = {"action_type": action_type, "parameters": parameters}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()
