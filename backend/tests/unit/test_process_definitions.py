from datetime import time

import pytest
from pydantic import ValidationError
from tiramisu_agents.builtin import load_fictional_deployment
from tiramisu_agents.processes.definitions import (
    DailyQuietHours,
    DefinitionStatus,
    ProcessDefinition,
)
from tiramisu_agents.processes.registry import AmbiguousTrigger, ProcessDefinitionRegistry


def test_example_process_definition_compiles_to_policy_and_instructions() -> None:
    deployment = load_fictional_deployment()
    registry = deployment.registry
    definition = deployment.definition

    assert definition.status is DefinitionStatus.PUBLISHED
    assert registry.resolve_trigger("enquiry.created") == definition
    assert registry.resolve_trigger("enquiry.created", include_drafts=True) == definition
    assert definition.decision_policy().max_actions_per_turn == 3
    assert definition.action_policy().permissions["send_message"] == "require_approval"
    assert "nonblank" in definition.action_guidance["send_message"]
    assert "Action parameter guidance" in definition.compile_instructions()
    assert "treat it as the customer's slot selection" in definition.compile_instructions()
    assert "Whenever you propose one or more actions, return status active" in (
        definition.compile_instructions()
    )
    assert len(definition.fingerprint()) == 64
    assert "Never claim" in definition.compile_instructions()
    assert "Maximum process lifetime: 90 days" in definition.compile_instructions()
    assert definition.communications.opt_out_event_types == ("customer.email_opted_out",)
    assert definition.communications.automated_response_event_types == (
        "customer.email_auto_replied",
    )


def test_quiet_hours_and_communication_event_roles_are_validated() -> None:
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        DailyQuietHours(
            timezone="Somewhere/Imaginary",
            start_local=time(20),
            end_local=time(8),
        )

    definition = load_fictional_deployment().definition
    document = definition.model_dump(mode="json")
    document["communications"]["reply_event_types"] = ["customer.email_received"]
    document["communications"]["opt_out_event_types"] = ["customer.email_received"]
    with pytest.raises(ValidationError, match="one unambiguous role"):
        ProcessDefinition.model_validate(document)

    document = definition.model_dump(mode="json")
    document["communications"]["quiet_hours"] = {
        "timezone": "Pacific/Auckland",
        "start_local": "20:00",
        "end_local": "08:00",
    }
    configured = ProcessDefinition.model_validate(document)
    assert configured.communications.quiet_hours is not None
    assert "Pacific/Auckland" in configured.compile_instructions()


def test_invalid_event_type_is_rejected() -> None:
    document = {
        "id": "example",
        "version": "1",
        "status": "draft",
        "trigger_events": ["Not Valid"],
        "goals": ["Do the thing"],
        "terminal_states": ["completed"],
        "allowed_actions": [],
        "action_permissions": {},
        "allowed_wake_events": [],
        "limits": {
            "max_actions_per_turn": 1,
            "max_follow_ups_without_reply": 1,
            "minimum_follow_up_interval_hours": 1,
            "maximum_timer_horizon_days": 1,
        },
    }
    with pytest.raises(ValidationError):
        ProcessDefinition.model_validate(document)


@pytest.mark.parametrize("field", ("trigger_events", "allowed_wake_events"))
def test_kernel_event_types_cannot_be_configured_as_business_events(field: str) -> None:
    document = {
        "id": "example",
        "version": "1",
        "status": "draft",
        "trigger_events": [],
        "goals": ["Do the thing"],
        "terminal_states": ["completed"],
        "allowed_actions": [],
        "action_permissions": {},
        "allowed_wake_events": [],
        "limits": {
            "max_actions_per_turn": 1,
            "max_follow_ups_without_reply": 1,
            "minimum_follow_up_interval_hours": 1,
            "maximum_timer_horizon_days": 1,
        },
    }
    document[field] = ["operator.manual_wake"]

    with pytest.raises(ValidationError, match="reserved for kernel use"):
        ProcessDefinition.model_validate(document)


def test_action_guidance_must_describe_an_allowed_action() -> None:
    document = {
        "id": "example",
        "version": "1",
        "status": "draft",
        "goals": ["Do the thing"],
        "terminal_states": ["completed"],
        "allowed_actions": ["send_message"],
        "action_permissions": {"send_message": "allow"},
        "action_guidance": {"delete_everything": "This must not be accepted."},
        "limits": {
            "max_actions_per_turn": 1,
            "max_follow_ups_without_reply": 1,
            "minimum_follow_up_interval_hours": 1,
            "maximum_timer_horizon_days": 1,
        },
    }

    with pytest.raises(ValidationError, match="action guidance"):
        ProcessDefinition.model_validate(document)


def test_ambiguous_enabled_trigger_is_rejected() -> None:
    draft = load_fictional_deployment().definition
    other = draft.model_copy(update={"id": "other_process"})
    registry = ProcessDefinitionRegistry([draft, other])

    with pytest.raises(AmbiguousTrigger):
        registry.resolve_trigger("enquiry.created", include_drafts=True)
