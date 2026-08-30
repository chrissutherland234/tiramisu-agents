"""Structured-output contracts for the bundled fictional process."""

import pytest
from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError
from tiramisu_agents.builtin.fictional_agent_output import FictionalAgentDecisionOutput


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
