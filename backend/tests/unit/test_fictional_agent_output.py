"""Structured-output contracts for the bundled fictional process."""

from typing import cast
from uuid import uuid4

import pytest
from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.core.contracts.actions import ActionAttemptStatus
from tiramisu_agents.core.contracts.knowledge import FactKind, FactObservation
from tiramisu_agents.core.contracts.processes import (
    ActionResultContext,
    AgentTurnInput,
    ProcessSnapshot,
    ProcessStatus,
)
from tiramisu_agents.projects import GeneratedAgentDecisionOutput

FictionalAgentDecisionOutput = cast(
    type[GeneratedAgentDecisionOutput],
    load_fictional_deployment().agent_decision_output_type,
)


def _send_message_action(parameters: dict[str, object]) -> dict[str, object]:
    return {
        "logical_action_key": "ask-for-details",
        "action_type": "send_message",
        "parameters": parameters,
        "rationale": "The customer needs a response.",
    }


def test_fictional_send_message_requires_exact_canonical_parameters() -> None:
    output = FictionalAgentDecisionOutput.model_validate(
        {
            "status": "active",
            "actions": [
                _send_message_action({"recipient": "customer@example.test", "body": "Hello"})
            ],
            "wake_conditions": [{"type": "event", "event_type": "customer.email_received"}],
        }
    )

    assert output.actions[0].to_action_proposal().parameters == {
        "recipient": "customer@example.test",
        "body": "Hello",
    }


def test_fictional_output_is_usable_as_a_strict_agents_sdk_schema() -> None:
    schema = AgentOutputSchema(FictionalAgentDecisionOutput)

    assert schema.is_plain_text() is False
    assert schema.json_schema()["additionalProperties"] is False


def test_fictional_output_rejects_standalone_human_wake() -> None:
    with pytest.raises(ValidationError):
        FictionalAgentDecisionOutput.model_validate(
            {
                "status": "waiting",
                "wake_conditions": [{"type": "human", "interaction": "approval"}],
            }
        )


def test_fictional_waiting_output_requires_a_wake() -> None:
    with pytest.raises(ValidationError):
        FictionalAgentDecisionOutput.model_validate({"status": "waiting"})


def test_fictional_completed_output_allows_no_wake() -> None:
    output = FictionalAgentDecisionOutput.model_validate({"status": "completed"})

    assert output.wake_conditions == ()


def test_fictional_output_adds_payment_after_confirmed_booking_result() -> None:
    booking_reference = "booking_demo_123"
    turn_input = AgentTurnInput(
        turn_id=uuid4(),
        process=ProcessSnapshot(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            process_type="enquiry_to_booking",
            process_definition_version="1",
            status=ProcessStatus.ACTIVE,
        ),
        events=(),
        action_results=(
            ActionResultContext(
                attempt_id=uuid4(),
                action_request_id=uuid4(),
                revision=1,
                action_type="propose_booking",
                parameters={"customer_id": "customer@example.test", "slot": "2026-09-02T10:00:00Z"},
                status=ActionAttemptStatus.SUCCEEDED,
                adapter_id="stub.booking.v1",
                idempotency_key="a" * 64,
                provider_reference=booking_reference,
                result={"booking_reference": booking_reference, "status": "confirmed"},
                facts=(
                    FactObservation(
                        key="booking.reference",
                        kind=FactKind.AUTHORITATIVE,
                        value=booking_reference,
                    ),
                    FactObservation(
                        key="booking.status",
                        kind=FactKind.AUTHORITATIVE,
                        value="confirmed",
                    ),
                ),
            ),
        ),
        instructions="test",
    )
    output = FictionalAgentDecisionOutput.model_validate(
        {
            "status": "active",
            "wake_conditions": [{"type": "event", "event_type": "payment.completed"}],
        }
    )

    decision = output.to_agent_decision(turn_input)

    assert len(decision.actions) == 1
    assert decision.actions[0].action_type == "request_payment"
    assert decision.actions[0].parameters == {
        "booking_reference": booking_reference,
        "amount_minor": 12_500,
        "currency": "NZD",
    }


def test_deterministic_payment_transition_reopens_a_completed_model_output() -> None:
    booking_reference = "booking_demo_completed"
    turn_input = AgentTurnInput(
        turn_id=uuid4(),
        process=ProcessSnapshot(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            process_type="enquiry_to_booking",
            process_definition_version="1",
            status=ProcessStatus.ACTIVE,
        ),
        events=(),
        action_results=(
            ActionResultContext(
                attempt_id=uuid4(),
                action_request_id=uuid4(),
                revision=1,
                action_type="propose_booking",
                parameters={},
                status=ActionAttemptStatus.SUCCEEDED,
                adapter_id="stub.booking.v1",
                idempotency_key="b" * 64,
                provider_reference=booking_reference,
                result={"booking_reference": booking_reference},
                facts=(
                    FactObservation(
                        key="booking.reference",
                        kind=FactKind.AUTHORITATIVE,
                        value=booking_reference,
                    ),
                    FactObservation(
                        key="booking.status",
                        kind=FactKind.AUTHORITATIVE,
                        value="confirmed",
                    ),
                ),
            ),
        ),
        instructions="test",
    )

    decision = FictionalAgentDecisionOutput.model_validate(
        {"status": "completed"}
    ).to_agent_decision(turn_input)

    assert decision.status.value == "active"
    assert [action.action_type for action in decision.actions] == ["request_payment"]


def test_fictional_output_drops_memory_summary_with_stale_provenance() -> None:
    turn_input = AgentTurnInput(
        turn_id=uuid4(),
        process=ProcessSnapshot(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            process_type="enquiry_to_booking",
            process_definition_version="1",
            status=ProcessStatus.ACTIVE,
        ),
        events=(),
        instructions="test",
    )
    output = FictionalAgentDecisionOutput.model_validate(
        {
            "status": "active",
            "wake_conditions": [{"type": "event", "event_type": "customer.email_received"}],
            "memory_update": {
                "summary": "Historical state",
                "summary_source_action_attempt_ids": [str(uuid4())],
                "open_commitments": ["Continue the process."],
            },
        }
    )

    decision = output.to_agent_decision(turn_input)

    assert decision.memory_update.summary is None
    assert decision.memory_update.open_commitments == ("Continue the process.",)


def test_fictional_output_drops_entire_summary_when_any_provenance_is_stale() -> None:
    event_id = uuid4()
    turn_input = AgentTurnInput(
        turn_id=uuid4(),
        process=ProcessSnapshot(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            process_type="enquiry_to_booking",
            process_definition_version="1",
            status=ProcessStatus.ACTIVE,
        ),
        events=(),
        timer_ids=("current-timer",),
        instructions="test",
    )
    output = FictionalAgentDecisionOutput.model_validate(
        {
            "status": "active",
            "wake_conditions": [{"type": "event", "event_type": "customer.email_received"}],
            "memory_update": {
                "summary": "Mixed current and historical claims",
                "summary_source_event_ids": [str(event_id)],
                "summary_source_timer_ids": ["current-timer"],
                "open_commitments": ["Continue safely."],
            },
        }
    )

    decision = output.to_agent_decision(turn_input)

    assert decision.memory_update.summary is None
    assert decision.memory_update.summary_source_timer_ids == ()
    assert decision.memory_update.open_commitments == ("Continue safely.",)


@pytest.mark.parametrize(
    "parameters",
    [
        {"recipient_email": "customer@example.test", "body": "Hello"},
        {"recipient": "", "body": "Hello"},
        {"recipient": "customer@example.test", "body": " "},
    ],
)
def test_fictional_send_message_rejects_incorrect_parameters(
    parameters: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        FictionalAgentDecisionOutput.model_validate(
            {"status": "active", "actions": [_send_message_action(parameters)]}
        )
