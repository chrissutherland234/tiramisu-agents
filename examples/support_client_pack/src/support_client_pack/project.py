"""Conventional project definition for a fictional support business."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tiramisu_agents.adapters.stubs import StubActionAdapter
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.extensions import ClientPack
from tiramisu_agents.projects import (
    Capability,
    Communications,
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
        version="2",
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
        communications=Communications(
            outbound_actions=(send_reply.action_type,),
            customer_reply_events=("customer.email_received",),
            opt_out_events=("customer.email_opted_out",),
            automated_response_events=("customer.email_auto_replied",),
        ),
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
                "customer.email_opted_out",
                journey=journey.id,
                title="Customer opted out",
                description="Stops further customer email for this case.",
            ),
            Route.wake(
                "customer.email_auto_replied",
                journey=journey.id,
                title="Automated response received",
                description="Stops follow-ups until a human reply arrives.",
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
                    ScenarioStep.event(
                        "case.created",
                        "A customer opens a support case.",
                        facts=(
                            CASE_STATUS.observed("open"),
                            CUSTOMER_EMAIL.observed("customer@example.test"),
                            CASE_SUBJECT.observed("Invoice total looks wrong"),
                        ),
                    ),
                    ScenarioStep.action(
                        "send_customer_reply",
                        "An operator approves a useful response.",
                        parameters={
                            "recipient": "customer@example.test",
                            "body": "Could you send us the invoice number?",
                        },
                        approve=True,
                    ),
                    ScenarioStep.wait_for_event(
                        "customer.email_received",
                        "The agent waits for the customer's invoice number.",
                    ),
                    ScenarioStep.event(
                        "customer.email_received",
                        "The customer provides the requested detail.",
                        facts=(
                            CUSTOMER_MESSAGE.observed(
                                "The invoice number is INV-1042.",
                                kind=FactKind.CUSTOMER_CLAIM,
                            ),
                        ),
                    ),
                    ScenarioStep.wait_for_event(
                        "case.resolved", "The agent waits for authoritative resolution."
                    ),
                    ScenarioStep.event(
                        "case.resolved",
                        "The support system resolves the case.",
                        facts=(CASE_STATUS.observed("resolved"),),
                    ),
                    ScenarioStep.fact(CASE_STATUS, "resolved", "Resolution is now authoritative."),
                    ScenarioStep.complete("The relationship agent completes."),
                ),
            ),
        ),
    )


def create_client_pack() -> ClientPack:
    return create_project().compile()
