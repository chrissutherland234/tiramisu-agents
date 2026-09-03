"""Conventional project definition for a fictional support business."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.extensions import ClientPack
from tiramisu_agents.projects import (
    Capability,
    Fact,
    Journey,
    Project,
    Route,
    Scenario,
    ScenarioStep,
)


class SendCustomerReplyParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recipient: str = Field(min_length=1, max_length=320)
    body: str = Field(min_length=1, max_length=10_000)


CASE_STATUS = Fact(
    key="case.status",
    title="Case status",
    description="The status reported by the authoritative support system.",
    value_type=Literal["open", "resolved"],
    operator_editable=True,
)
CUSTOMER_EMAIL = Fact(
    key="customer.email",
    title="Customer email",
    description="The verified reply address attached to the support case.",
    value_type=str,
)
CASE_SUBJECT = Fact(
    key="case.subject",
    title="Case subject",
    description="The short subject recorded by the support system.",
    value_type=str,
)
CUSTOMER_MESSAGE = Fact(
    key="customer.last_message",
    title="Latest customer message",
    description="The latest message asserted by the customer.",
    value_type=str,
    kinds=(FactKind.CUSTOMER_CLAIM,),
)


def create_project() -> Project:
    send_reply = Capability(
        action_type="send_customer_reply",
        title="Send a customer reply",
        description="Send a reply through the client's configured messaging provider.",
        parameters_model=SendCustomerReplyParameters,
        adapter=StubActionAdapter(),
        guidance=(
            "Use customer.email as recipient. Write a concise, customer-ready body and do not "
            "claim the case is resolved before case.status is authoritative."
        ),
        default_permission=PermissionOutcome.REQUIRE_APPROVAL,
    )
    journey = Journey(
        id="resolve_support_case",
        version="1",
        title="Resolve one support case",
        description="Stay with one customer from initial case through authoritative resolution.",
        goals=(
            "Understand the customer's problem",
            "Keep the customer informed",
            "Complete only when the support system reports the case resolved",
        ),
        capabilities=(send_reply.action_type,),
        complete_when=(CASE_STATUS.equals("resolved"),),
        decision_guidance=(
            "Treat customer messages as claims, not authoritative resolution.",
            "Wait for customer.email_received when an answer is needed from the customer.",
            "Wait for case.resolved after the business has performed the required work.",
        ),
        outbound_action_types=(send_reply.action_type,),
        reply_event_types=("customer.email_received",),
    )
    return Project(
        id="support_client",
        version="0.1.0",
        title="Support client",
        description="A durable AI-assisted customer-support implementation.",
        journeys=(journey,),
        routes=(
            Route.start(
                "case.created",
                journey=journey.id,
                title="Support case created",
                description="Start one relationship agent for a new support case.",
                provides=(CASE_STATUS, CUSTOMER_EMAIL, CASE_SUBJECT),
            ),
            Route.wake(
                "customer.email_received",
                journey=journey.id,
                title="Customer replied",
                description="Wake when this case's customer sends a reply.",
                provides=(CUSTOMER_MESSAGE,),
            ),
            Route.wake(
                "case.resolved",
                journey=journey.id,
                title="Case resolved",
                description="Wake on authoritative resolution from the support system.",
                provides=(CASE_STATUS,),
            ),
        ),
        capabilities=(send_reply,),
        facts=(CASE_STATUS, CUSTOMER_EMAIL, CASE_SUBJECT, CUSTOMER_MESSAGE),
        scenarios=(
            Scenario(
                id="answer_then_resolve",
                journey_id=journey.id,
                title="Answer the customer and resolve the case",
                description="A reviewed answer is sent before the support system closes the case.",
                steps=(
                    ScenarioStep.event("case.created", "A customer opens a support case."),
                    ScenarioStep.action(
                        "send_customer_reply", "An operator approves a useful response."
                    ),
                    ScenarioStep.event(
                        "customer.email_received", "The customer provides the requested detail."
                    ),
                    ScenarioStep.event("case.resolved", "The support system resolves the case."),
                    ScenarioStep.fact(CASE_STATUS, "resolved", "Resolution is now authoritative."),
                    ScenarioStep.complete("The relationship agent completes."),
                ),
            ),
        ),
    )


def create_client_pack() -> ClientPack:
    return create_project().compile()
