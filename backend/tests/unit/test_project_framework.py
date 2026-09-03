"""Author-facing project compilation and generated contract tests."""

from typing import Literal, cast
from uuid import uuid4

import pytest
from agents.agent_output import AgentOutputSchema
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.core.contracts.processes import AgentTurnInput, ProcessSnapshot, ProcessStatus
from tiramisu_agents.projects import (
    Capability,
    Fact,
    GeneratedAgentDecisionOutput,
    Journey,
    Project,
    ProjectConfigurationError,
    Route,
    Scenario,
    ScenarioStep,
)


class SendReplyParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recipient: str = Field(min_length=1)
    message: str = Field(min_length=1)


CASE_STATUS = Fact(
    key="case.status",
    title="Case status",
    description="The authoritative support case status.",
    value_type=Literal["open", "resolved"],
    operator_editable=True,
)
CUSTOMER_MESSAGE = Fact(
    key="customer.message",
    title="Customer message",
    description="The customer's latest reply.",
    value_type=str,
    kinds=(FactKind.CUSTOMER_CLAIM,),
)


def _project() -> Project:
    capability = Capability(
        action_type="send_reply",
        title="Send reply",
        description="Reply to the customer through the configured provider.",
        parameters_model=SendReplyParameters,
        adapter=StubActionAdapter(),
        guidance="Use a known recipient and a concise message.",
        default_permission=PermissionOutcome.REQUIRE_APPROVAL,
    )
    journey = Journey(
        id="support_case",
        version="1",
        title="Resolve a support case",
        description="Follow one support case until it is authoritatively resolved.",
        goals=("Resolve the customer's problem",),
        capabilities=(capability.action_type,),
        complete_when=(CASE_STATUS.equals("resolved"),),
        outbound_action_types=(capability.action_type,),
        reply_event_types=("customer.replied",),
    )
    return Project(
        id="support_demo",
        version="0.1.0",
        title="Support demo",
        description="A small support-case project.",
        journeys=(journey,),
        routes=(
            Route.start(
                "case.created",
                journey=journey.id,
                title="Case created",
                description="Starts one agent for a new support case.",
                provides=(CASE_STATUS,),
            ),
            Route.wake(
                "customer.replied",
                journey=journey.id,
                title="Customer replied",
                description="Wakes the case when its customer replies.",
                provides=(CUSTOMER_MESSAGE,),
            ),
        ),
        capabilities=(capability,),
        facts=(CASE_STATUS, CUSTOMER_MESSAGE),
        scenarios=(
            Scenario(
                id="happy_path",
                journey_id=journey.id,
                title="Case resolved",
                description="A customer gets an answer and the case is resolved.",
                steps=(
                    ScenarioStep.event("case.created", "A support case is created."),
                    ScenarioStep.action("send_reply", "The agent sends a reviewed reply."),
                    ScenarioStep.fact(CASE_STATUS, "resolved", "The case is resolved."),
                    ScenarioStep.complete("The agent completes the journey."),
                ),
            ),
        ),
    )


def test_project_compiles_all_runtime_and_business_metadata() -> None:
    pack = _project().compile()

    assert pack.manifest.extension_id == "support_demo"
    assert pack.manifest.process_definitions == ("support_case.v1",)
    assert pack.policy_ids == ("support_demo.support_case.policy.v1",)
    assert pack.definition.trigger_events == ("case.created",)
    assert pack.definition.allowed_wake_events == ("customer.replied",)
    assert pack.definition.completion_requirements == {"case.status": "resolved"}
    assert pack.definition.integrations == {"send_reply": "stub.actions.v1"}
    assert pack.project is not None
    assert pack.project.journeys[0].scenarios[0].title == "Case resolved"
    assert pack.project.journeys[0].facts[0].operator_editable is True
    assert "case.status must equal" in pack.definition.compile_instructions()


def test_generated_output_is_strict_and_converts_to_a_trusted_decision() -> None:
    output_type = cast(
        type[GeneratedAgentDecisionOutput],
        _project().compile().agent_decision_output_type,
    )
    schema = AgentOutputSchema(output_type)
    output = output_type.model_validate(
        {
            "status": "active",
            "actions": [
                {
                    "logical_action_key": "reply-once",
                    "action_type": "send_reply",
                    "parameters": {
                        "recipient": "person@example.test",
                        "message": "We have fixed this.",
                    },
                    "rationale": "Tell the customer the outcome.",
                }
            ],
            "wake_conditions": [{"type": "event", "event_type": "customer.replied"}],
        }
    )
    turn_input = AgentTurnInput(
        turn_id=uuid4(),
        process=ProcessSnapshot(
            tenant_id=uuid4(),
            process_instance_id=uuid4(),
            process_type="support_case",
            process_definition_version="1",
            status=ProcessStatus.ACTIVE,
        ),
        events=(),
        instructions="Resolve the support case.",
    )

    decision = output.to_agent_decision(turn_input)

    assert schema.is_plain_text() is False
    assert schema.json_schema()["additionalProperties"] is False
    assert output_type.__name__ == "SupportDemoAgentDecisionOutputV010"
    assert decision.actions[0].parameters == {
        "recipient": "person@example.test",
        "message": "We have fixed this.",
    }
    assert decision.based_on_event_ids == ()


_INVALID_OUTPUTS: tuple[dict[str, object], ...] = (
    {
        "status": "active",
        "actions": [
            {
                "logical_action_key": "wrong-action",
                "action_type": "delete_case",
                "parameters": cast(dict[str, object], {}),
                "rationale": "Not registered.",
            }
        ],
    },
    {
        "status": "waiting",
        "wake_conditions": [{"type": "event", "event_type": "payment.completed"}],
    },
    {
        "status": "waiting",
        "wake_conditions": [{"type": "human", "interaction": "approval"}],
    },
)


@pytest.mark.parametrize("document", _INVALID_OUTPUTS)
def test_generated_output_rejects_unregistered_actions_and_wakes(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _project().compile().agent_decision_output_type.model_validate(document)


def test_project_rejects_completion_based_on_a_customer_claim() -> None:
    project = _project()
    unsafe = project.journeys[0]
    unsafe = Journey(
        id=unsafe.id,
        version=unsafe.version,
        title=unsafe.title,
        description=unsafe.description,
        goals=unsafe.goals,
        capabilities=unsafe.capabilities,
        complete_when=(CUSTOMER_MESSAGE.equals("done"),),
    )

    with pytest.raises(ProjectConfigurationError, match="authoritative"):
        Project(
            id=project.id,
            version=project.version,
            title=project.title,
            description=project.description,
            journeys=(unsafe,),
            routes=project.routes,
            capabilities=project.capabilities,
            facts=project.facts,
        ).compile()


def test_observation_only_journey_generates_an_empty_action_contract() -> None:
    journey = Journey(
        id="monitor_case",
        version="1",
        title="Monitor a case",
        description="Wait until a provider reports that the case is resolved.",
        goals=("Observe the case to completion",),
        capabilities=(),
        complete_when=(CASE_STATUS.equals("resolved"),),
    )
    pack = Project(
        id="monitor",
        version="1",
        title="Monitor",
        description="An action-free monitoring project.",
        journeys=(journey,),
        routes=(
            Route.start(
                "case.created",
                journey=journey.id,
                title="Case created",
                description="Start monitoring a case.",
                provides=(CASE_STATUS,),
            ),
        ),
        capabilities=(),
        facts=(CASE_STATUS,),
    ).compile()

    with pytest.raises(ValidationError):
        pack.agent_decision_output_type.model_validate(
            {
                "status": "active",
                "actions": [{"action_type": "anything"}],
            }
        )


def test_project_can_compile_multiple_journeys_with_shared_capabilities() -> None:
    project = _project()
    escalation = Journey(
        id="priority_case",
        version="1",
        title="Resolve a priority case",
        description="Handle a priority support case under the same client deployment.",
        goals=("Resolve the priority case",),
        capabilities=("send_reply",),
        complete_when=(CASE_STATUS.equals("resolved"),),
    )
    pack = Project(
        id=project.id,
        version=project.version,
        title=project.title,
        description=project.description,
        journeys=(*project.journeys, escalation),
        routes=(
            *project.routes,
            Route.start(
                "priority_case.created",
                journey=escalation.id,
                title="Priority case created",
                description="Starts the priority support journey.",
                provides=(CASE_STATUS,),
            ),
        ),
        capabilities=project.capabilities,
        facts=project.facts,
        scenarios=project.scenarios,
    ).compile()

    assert tuple(definition.id for definition in pack.definitions) == (
        "support_case",
        "priority_case",
    )
    assert pack.manifest.process_definitions == (
        "support_case.v1",
        "priority_case.v1",
    )
    assert set(pack.bindings) == {"send_reply"}


def test_project_rejects_an_ambiguous_published_start_route() -> None:
    project = _project()
    duplicate = Journey(
        id="duplicate_case",
        version="1",
        title="Duplicate case",
        description="An invalid journey with an ambiguous start.",
        goals=("Demonstrate trigger validation",),
        capabilities=("send_reply",),
        complete_when=(CASE_STATUS.equals("resolved"),),
    )

    with pytest.raises(ProjectConfigurationError, match="more than one journey"):
        Project(
            id=project.id,
            version=project.version,
            title=project.title,
            description=project.description,
            journeys=(*project.journeys, duplicate),
            routes=(
                *project.routes,
                Route.start(
                    "case.created",
                    journey=duplicate.id,
                    title="Same case created",
                    description="Incorrectly competes for the same published event.",
                    provides=(CASE_STATUS,),
                ),
            ),
            capabilities=project.capabilities,
            facts=project.facts,
            scenarios=project.scenarios,
        ).compile()
