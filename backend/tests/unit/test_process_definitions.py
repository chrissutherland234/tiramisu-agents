from pathlib import Path

import pytest
from pydantic import ValidationError
from tiramisu_agents.processes.definitions import DefinitionStatus, ProcessDefinition
from tiramisu_agents.processes.registry import AmbiguousTrigger, ProcessDefinitionRegistry

EXAMPLE_PATH = Path("process_definitions/examples/enquiry_to_booking.v1.yaml")


def test_example_process_definition_compiles_to_policy_and_instructions() -> None:
    registry = ProcessDefinitionRegistry.from_yaml_files([EXAMPLE_PATH])
    definition = registry.get("enquiry_to_booking", "1")

    assert definition.status is DefinitionStatus.DRAFT
    assert registry.resolve_trigger("enquiry.created") is None
    assert registry.resolve_trigger("enquiry.created", include_drafts=True) == definition
    assert definition.decision_policy().max_actions_per_turn == 3
    assert len(definition.fingerprint()) == 64
    assert "Never claim" in definition.compile_instructions()


def test_invalid_event_type_is_rejected() -> None:
    document = {
        "id": "example",
        "version": "1",
        "status": "draft",
        "trigger_events": ["Not Valid"],
        "goals": ["Do the thing"],
        "terminal_states": ["completed"],
        "allowed_actions": [],
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


def test_ambiguous_enabled_trigger_is_rejected() -> None:
    draft = ProcessDefinitionRegistry.from_yaml_files([EXAMPLE_PATH]).get("enquiry_to_booking", "1")
    other = draft.model_copy(update={"id": "other_process"})
    registry = ProcessDefinitionRegistry([draft, other])

    with pytest.raises(AmbiguousTrigger):
        registry.resolve_trigger("enquiry.created", include_drafts=True)
