"""Hard platform ceilings for untrusted data and bounded agent context."""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, fields
from typing import Any


class SafetyLimitExceeded(ValueError):
    """Raised when data exceeds a deterministic platform safety ceiling."""

    def __init__(self, label: str, *, actual: int, limit: int, unit: str) -> None:
        self.label = label
        self.actual = actual
        self.limit = limit
        self.unit = unit
        super().__init__(f"{label} exceeds platform limit of {limit} {unit} (received {actual})")


@dataclass(frozen=True, slots=True)
class PlatformSafetyLimits:
    """Conservative hard maxima; client policy may eventually choose lower values."""

    max_event_payload_fields: int = 100
    max_event_payload_bytes: int = 64 * 1024
    max_event_input_bytes: int = 96 * 1024
    max_canonical_event_bytes: int = 128 * 1024
    max_external_references_per_event: int = 20
    max_facts_per_event: int = 50
    max_fact_value_bytes: int = 16 * 1024

    max_events_per_turn: int = 50
    max_reviews_per_turn: int = 20
    max_action_results_per_turn: int = 20
    max_timers_per_turn: int = 50

    max_action_parameter_fields: int = 100
    max_action_parameters_bytes: int = 32 * 1024
    max_review_message_bytes: int = 16 * 1024
    max_operator_guidance_bytes: int = 16 * 1024

    max_open_commitments: int = 50
    max_commitment_bytes: int = 2 * 1024
    max_open_commitments_bytes: int = 32 * 1024
    max_memory_summary_bytes: int = 16 * 1024

    max_process_fact_entries: int = 500
    max_process_fact_projection_bytes: int = 128 * 1024
    max_agent_context_bytes: int = 256 * 1024
    max_rendered_prompt_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{item.name} must be a positive integer")
        if self.max_event_payload_bytes > self.max_event_input_bytes:
            raise ValueError("event payload limit cannot exceed event input limit")
        if self.max_event_input_bytes > self.max_canonical_event_bytes:
            raise ValueError("event input limit cannot exceed canonical event limit")
        if self.max_agent_context_bytes > self.max_rendered_prompt_bytes:
            raise ValueError("agent context limit cannot exceed rendered prompt limit")


DEFAULT_PLATFORM_SAFETY_LIMITS = PlatformSafetyLimits()


def canonical_json_bytes(value: Any, *, label: str) -> bytes:
    """Encode JSON deterministically and reject values PostgreSQL JSON cannot safely represent."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError(f"{label} must contain JSON-compatible values") from error


def require_json_bytes(value: Any, *, label: str, max_bytes: int) -> int:
    actual = len(canonical_json_bytes(value, label=label))
    if actual > max_bytes:
        raise SafetyLimitExceeded(label, actual=actual, limit=max_bytes, unit="bytes")
    return actual


def require_utf8_bytes(value: str, *, label: str, max_bytes: int) -> int:
    actual = len(value.encode("utf-8"))
    if actual > max_bytes:
        raise SafetyLimitExceeded(label, actual=actual, limit=max_bytes, unit="bytes")
    return actual


def require_item_count(value: Collection[object], *, label: str, max_items: int) -> int:
    actual = len(value)
    if actual > max_items:
        raise SafetyLimitExceeded(label, actual=actual, limit=max_items, unit="items")
    return actual


def require_event_content(
    *,
    payload: Mapping[str, Any],
    external_references: Collection[object],
    facts: Collection[object],
    limits: PlatformSafetyLimits = DEFAULT_PLATFORM_SAFETY_LIMITS,
) -> None:
    require_item_count(
        payload,
        label="event payload fields",
        max_items=limits.max_event_payload_fields,
    )
    require_json_bytes(
        payload,
        label="event payload",
        max_bytes=limits.max_event_payload_bytes,
    )
    require_item_count(
        external_references,
        label="event external references",
        max_items=limits.max_external_references_per_event,
    )
    require_item_count(
        facts,
        label="event facts",
        max_items=limits.max_facts_per_event,
    )


def require_action_parameters(
    parameters: Mapping[str, Any],
    *,
    label: str = "action parameters",
    limits: PlatformSafetyLimits = DEFAULT_PLATFORM_SAFETY_LIMITS,
) -> None:
    require_item_count(
        parameters,
        label=f"{label} fields",
        max_items=limits.max_action_parameter_fields,
    )
    require_json_bytes(
        parameters,
        label=label,
        max_bytes=limits.max_action_parameters_bytes,
    )


def require_memory_content(
    *,
    summary: str | None,
    open_commitments: Collection[str],
    limits: PlatformSafetyLimits = DEFAULT_PLATFORM_SAFETY_LIMITS,
) -> None:
    if summary is not None:
        require_utf8_bytes(
            summary,
            label="memory summary",
            max_bytes=limits.max_memory_summary_bytes,
        )
    require_item_count(
        open_commitments,
        label="open commitments",
        max_items=limits.max_open_commitments,
    )
    for commitment in open_commitments:
        require_utf8_bytes(
            commitment,
            label="open commitment",
            max_bytes=limits.max_commitment_bytes,
        )
    require_json_bytes(
        open_commitments,
        label="open commitments",
        max_bytes=limits.max_open_commitments_bytes,
    )


def require_process_fact_projection(
    *,
    authoritative_facts: Mapping[str, Any],
    customer_claims: Mapping[str, Any],
    fact_provenance: Mapping[str, Any],
    limits: PlatformSafetyLimits = DEFAULT_PLATFORM_SAFETY_LIMITS,
) -> None:
    entry_count = len(authoritative_facts) + len(customer_claims)
    if entry_count > limits.max_process_fact_entries:
        raise SafetyLimitExceeded(
            "process fact projection",
            actual=entry_count,
            limit=limits.max_process_fact_entries,
            unit="items",
        )
    require_json_bytes(
        {
            "authoritative_facts": authoritative_facts,
            "customer_claims": customer_claims,
            "fact_provenance": fact_provenance,
        },
        label="process fact projection",
        max_bytes=limits.max_process_fact_projection_bytes,
    )
