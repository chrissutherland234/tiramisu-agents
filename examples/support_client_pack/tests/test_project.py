"""The standalone package compiles without relying on the booking example."""

import asyncio

import pytest
from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError
from tiramisu_agents.testkit import run_scenario

from support_client_pack import create_client_pack


def test_support_project_compiles_to_a_safe_client_pack() -> None:
    pack = create_client_pack()

    assert pack.manifest.extension_id == "support_client"
    assert pack.definition.id == "resolve_support_case"
    assert pack.definition.version == "2"
    assert pack.definition.trigger_events == ("case.created",)
    assert pack.definition.allowed_wake_events == (
        "customer.email_received",
        "customer.email_opted_out",
        "customer.email_auto_replied",
        "case.resolved",
    )
    assert pack.definition.completion_requirements == {"case.status": "resolved"}
    assert "booking" not in pack.definition.compile_instructions().lower()
    assert AgentOutputSchema(pack.agent_decision_output_type).is_plain_text() is False


def test_support_output_only_accepts_its_registered_capability() -> None:
    output_type = create_client_pack().agent_decision_output_type
    valid = output_type.model_validate(
        {
            "status": "active",
            "actions": [
                {
                    "logical_action_key": "reply-to-case",
                    "action_type": "send_customer_reply",
                    "parameters": {
                        "recipient": "customer@example.test",
                        "body": "Could you send the error number?",
                    },
                    "rationale": "One detail is needed to investigate.",
                }
            ],
        }
    )
    assert valid.model_dump(mode="json")["actions"][0]["action_type"] == ("send_customer_reply")

    with pytest.raises(ValidationError):
        output_type.model_validate(
            {
                "status": "active",
                "actions": [
                    {
                        "logical_action_key": "book-something",
                        "action_type": "propose_booking",
                        "parameters": {},
                        "rationale": "This project cannot do that.",
                    }
                ],
            }
        )


def test_support_scenario_executes_to_authoritative_resolution() -> None:
    result = asyncio.run(run_scenario(create_client_pack(), "answer_then_resolve"))

    assert result.passed is True
    assert result.action_types == ("send_customer_reply",)
    assert result.approval_count == 1
    assert result.authoritative_facts["case.status"] == "resolved"
