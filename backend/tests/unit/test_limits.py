"""Boundary tests for the deterministic platform safety envelope."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    MemoryUpdate,
)
from tiramisu_agents.core.contracts.events import CanonicalEvent
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.reviews import ReviewCommand, ReviewCommandType
from tiramisu_agents.core.limits import (
    DEFAULT_PLATFORM_SAFETY_LIMITS,
    PlatformSafetyLimits,
    canonical_json_bytes,
    require_process_fact_projection,
)
from tiramisu_agents.core.policy import DecisionPolicy, DecisionRejected, validate_decision


def _json_string_for_exact_size(*, key: str, byte_limit: int) -> str:
    overhead = len(canonical_json_bytes({key: ""}, label="test payload"))
    return "x" * (byte_limit - overhead)


def test_event_payload_accepts_the_byte_limit_and_rejects_one_byte_over() -> None:
    limit = DEFAULT_PLATFORM_SAFETY_LIMITS.max_event_payload_bytes
    payload_value = _json_string_for_exact_size(key="body", byte_limit=limit)
    tenant_id = uuid4()

    def event_with_payload(value: str) -> CanonicalEvent:
        return CanonicalEvent(
            tenant_id=tenant_id,
            event_type="enquiry.created",
            source="website",
            source_event_id="enquiry-1",
            occurred_at=datetime.now(UTC),
            payload={"body": value},
        )

    accepted = event_with_payload(payload_value)

    assert len(canonical_json_bytes(accepted.payload, label="event payload")) == limit
    with pytest.raises(ValidationError, match="event payload exceeds platform limit"):
        event_with_payload(f"{payload_value}x")


def test_fact_value_accepts_the_byte_limit_and_rejects_one_byte_over() -> None:
    limit = DEFAULT_PLATFORM_SAFETY_LIMITS.max_fact_value_bytes
    value = "x" * (limit - 2)  # JSON string quotes account for two bytes.

    accepted = FactObservation(
        key="payment.status",
        kind=FactKind.AUTHORITATIVE,
        value=value,
    )

    assert len(canonical_json_bytes(accepted.value, label="fact value")) == limit
    with pytest.raises(ValidationError, match="fact value exceeds platform limit"):
        FactObservation(
            key="payment.status",
            kind=FactKind.AUTHORITATIVE,
            value=f"{value}x",
        )


def test_fact_values_must_be_valid_postgresql_json() -> None:
    with pytest.raises(ValidationError, match="JSON-compatible"):
        FactObservation(
            key="payment.amount",
            kind=FactKind.AUTHORITATIVE,
            value=float("nan"),
        )


def test_review_message_uses_utf8_bytes_not_character_count() -> None:
    limits = DEFAULT_PLATFORM_SAFETY_LIMITS
    accepted_message = "é" * (limits.max_review_message_bytes // 2)
    tenant_id = uuid4()
    process_instance_id = uuid4()
    review_thread_id = uuid4()
    action_request_id = uuid4()
    actor_id = uuid4()

    def command_with_message(message: str) -> ReviewCommand:
        return ReviewCommand(
            tenant_id=tenant_id,
            process_instance_id=process_instance_id,
            review_thread_id=review_thread_id,
            action_request_id=action_request_id,
            proposal_revision=1,
            command_type=ReviewCommandType.REQUEST_REVISION,
            actor_id=actor_id,
            message=message,
        )

    accepted = command_with_message(accepted_message)

    assert accepted.message is not None
    assert len(accepted.message.encode("utf-8")) == limits.max_review_message_bytes
    with pytest.raises(ValidationError, match="review message exceeds platform limit"):
        command_with_message(f"{accepted_message}é")


def test_policy_rejects_oversized_action_parameters_with_repairable_feedback() -> None:
    parameters = {"body": "x" * 20}
    decision = AgentDecision(
        based_on_event_ids=(),
        status=DecisionStatus.ACTIVE,
        actions=(
            ActionProposal(
                logical_action_key="send_follow_up",
                action_type="send_message",
                parameters=parameters,
                rationale="Follow up with the customer.",
            ),
        ),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset({"send_message"}),
        allowed_wake_event_types=frozenset(),
        max_action_parameters_bytes=len(
            canonical_json_bytes(parameters, label="test action parameters")
        )
        - 1,
    )

    with pytest.raises(DecisionRejected, match="action parameters.*exceeds platform limit"):
        validate_decision(decision, policy, workflow_now=datetime.now(UTC))


def test_policy_rejects_oversized_persistent_commitment_memory() -> None:
    decision = AgentDecision(
        based_on_event_ids=(),
        status=DecisionStatus.WAITING,
        wake_conditions=(EventWakeCondition(event_type="customer.email_received"),),
        memory_update=MemoryUpdate(open_commitments=("one", "two")),
    )
    policy = DecisionPolicy(
        allowed_action_types=frozenset(),
        allowed_wake_event_types=frozenset({"customer.email_received"}),
        max_open_commitments=1,
    )

    with pytest.raises(DecisionRejected, match="open commitments exceeds platform limit"):
        validate_decision(decision, policy, workflow_now=datetime.now(UTC))


def test_process_fact_projection_enforces_combined_entry_count() -> None:
    limits = PlatformSafetyLimits(max_process_fact_entries=1)

    with pytest.raises(ValueError, match="process fact projection exceeds platform limit"):
        require_process_fact_projection(
            authoritative_facts={"payment.status": "paid"},
            customer_claims={"customer.preference": "morning"},
            fact_provenance={},
            limits=limits,
        )


def test_platform_limits_reject_invalid_relationships() -> None:
    with pytest.raises(ValueError, match="payload limit cannot exceed event input limit"):
        PlatformSafetyLimits(
            max_event_payload_bytes=10,
            max_event_input_bytes=9,
        )


def test_process_policy_cannot_raise_a_platform_safety_maximum() -> None:
    with pytest.raises(ValueError, match="cannot exceed the platform maximum"):
        DecisionPolicy(
            allowed_action_types=frozenset(),
            allowed_wake_event_types=frozenset(),
            max_action_parameters_bytes=(
                DEFAULT_PLATFORM_SAFETY_LIMITS.max_action_parameters_bytes + 1
            ),
        )
