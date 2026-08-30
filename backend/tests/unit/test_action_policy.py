import pytest
from pydantic import ValidationError
from tiramisu_agents.actions.gateway import action_payload_hash
from tiramisu_agents.core.action_policy import ConfiguredActionPolicy
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.decisions import ActionProposal
from tiramisu_agents.processes.definitions import ProcessDefinition


def test_action_hash_is_canonical_and_excludes_explanation() -> None:
    first = ActionProposal(
        logical_action_key="first",
        action_type="send_message",
        parameters={"recipient": "person@example.test", "content": {"b": 2, "a": 1}},
        rationale="First explanation.",
    )
    second = ActionProposal(
        logical_action_key="second",
        action_type="send_message",
        parameters={"content": {"a": 1, "b": 2}, "recipient": "person@example.test"},
        rationale="A different explanation.",
    )

    assert action_payload_hash(first) == action_payload_hash(second)


def test_unconfigured_action_fails_closed() -> None:
    policy = ConfiguredActionPolicy(permissions={}, version="test")
    action = ActionProposal(
        logical_action_key="unsafe",
        action_type="unknown_action",
        rationale="Exercise the default.",
    )

    assert policy.evaluate(action).outcome is PermissionOutcome.DENY


def test_process_definition_requires_a_permission_for_every_action() -> None:
    with pytest.raises(ValidationError, match="exactly one permission"):
        ProcessDefinition.model_validate(
            {
                "id": "example",
                "version": "1",
                "status": "draft",
                "goals": ["Test explicit permissions"],
                "terminal_states": ["completed"],
                "allowed_actions": ["send_message"],
                "action_permissions": {},
                "limits": {
                    "max_actions_per_turn": 1,
                    "max_follow_ups_without_reply": 1,
                    "minimum_follow_up_interval_hours": 1,
                    "maximum_timer_horizon_days": 1,
                },
            }
        )
