"""Bundled fictional project expressed through the public authoring framework."""

from datetime import UTC, datetime
from typing import Literal

from tiramisu_agents.adapters.stubs import StubBusinessState, stub_business_bindings
from tiramisu_agents.builtin.fictional_agent_output import (
    CreateCalendarEventParameters,
    FindAvailableSlotsParameters,
    ProposeBookingParameters,
    RequestPaymentParameters,
    SendMessageParameters,
    apply_fictional_transitions,
)
from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.knowledge import FactKind
from tiramisu_agents.extensions import ClientPack, ClientPackError
from tiramisu_agents.projects import (
    Capability,
    Fact,
    Journey,
    Project,
    Route,
    Scenario,
    ScenarioStep,
    ScenarioValue,
)


class FictionalDeploymentError(ClientPackError):
    """Raised when the bundled project cannot compile into a safe deployment."""


FictionalDeployment = ClientPack
FICTIONAL_SCENARIO_STARTED_AT = datetime(2026, 1, 1, 9, tzinfo=UTC)


CUSTOMER_EMAIL = Fact(
    key="customer.email",
    title="Customer email",
    description="The verified email address attached to the enquiry.",
    value_type=str,
)
CUSTOMER_LAST_MESSAGE = Fact(
    key="customer.last_message",
    title="Latest customer message",
    description="The customer's most recent inbound message, retained as a claim.",
    value_type=str,
    kinds=(FactKind.CUSTOMER_CLAIM,),
)
OUTBOUND_MESSAGE_REFERENCE = Fact(
    key="messaging.last_outbound_reference",
    title="Latest outbound message",
    description="The provider reference for the most recently sent message.",
    value_type=str,
)
AVAILABLE_SLOTS = Fact(
    key="booking.available_slots",
    title="Available booking slots",
    description="Appointment slots reported available by the booking provider.",
    value_type=list[str],
)
BOOKING_REFERENCE = Fact(
    key="booking.reference",
    title="Booking reference",
    description="The booking provider's durable reference.",
    value_type=str,
)
BOOKING_STATUS = Fact(
    key="booking.status",
    title="Booking status",
    description="The booking status reported by the booking provider.",
    value_type=Literal["confirmed", "cancelled"],
    operator_editable=True,
)
BOOKING_SLOT = Fact(
    key="booking.slot",
    title="Confirmed booking slot",
    description="The exact start time confirmed by the booking provider.",
    value_type=str,
)
PAYMENT_REFERENCE = Fact(
    key="payment.reference",
    title="Payment reference",
    description="The payment provider's durable reference.",
    value_type=str,
)
PAYMENT_STATUS = Fact(
    key="payment.status",
    title="Payment status",
    description="The payment state reported by the authoritative payment integration.",
    value_type=Literal["pending", "completed", "failed"],
    operator_editable=True,
)
PAYMENT_AMOUNT_MINOR = Fact(
    key="payment.amount_minor",
    title="Payment amount",
    description="The requested amount in the currency's minor unit.",
    value_type=int,
)
PAYMENT_CURRENCY = Fact(
    key="payment.currency",
    title="Payment currency",
    description="The ISO currency code for the payment.",
    value_type=Literal["NZD"],
)
CALENDAR_REFERENCE = Fact(
    key="calendar.reference",
    title="Calendar event reference",
    description="The calendar provider's durable event reference.",
    value_type=str,
)
CALENDAR_STATUS = Fact(
    key="calendar.status",
    title="Calendar status",
    description="Whether the confirmed booking has been added to the calendar.",
    value_type=Literal["created"],
    operator_editable=True,
)


def create_fictional_project(*, state: StubBusinessState | None = None) -> Project:
    """Build the example project with adapters bound to one deterministic provider state."""

    bindings = stub_business_bindings(state or StubBusinessState())
    simulation_bindings = (
        bindings
        if state is not None
        else stub_business_bindings(StubBusinessState(now=FICTIONAL_SCENARIO_STARTED_AT))
    )
    capabilities = (
        Capability(
            action_type="send_message",
            title="Send a customer message",
            description="Send a plain-text message through the configured messaging provider.",
            parameters_model=SendMessageParameters,
            adapter=bindings["send_message"],
            simulation_adapter=simulation_bindings["send_message"],
            guidance=(
                "Use the exact keys recipient and body. Both values must be nonblank, and body "
                "must be customer-ready plain text."
            ),
            default_permission=PermissionOutcome.REQUIRE_APPROVAL,
            produces=(OUTBOUND_MESSAGE_REFERENCE,),
        ),
        Capability(
            action_type="find_available_slots",
            title="Find available slots",
            description="Ask the booking provider for open appointment times.",
            parameters_model=FindAvailableSlotsParameters,
            adapter=bindings["find_available_slots"],
            simulation_adapter=simulation_bindings["find_available_slots"],
            guidance="Use a positive integer days parameter describing the search horizon.",
            default_permission=PermissionOutcome.ALLOW,
            produces=(AVAILABLE_SLOTS,),
        ),
        Capability(
            action_type="propose_booking",
            title="Confirm a booking",
            description="Book an authoritative available slot for the customer.",
            parameters_model=ProposeBookingParameters,
            adapter=bindings["propose_booking"],
            simulation_adapter=simulation_bindings["propose_booking"],
            guidance=(
                "Use nonblank customer_id and slot values. slot must be present in the current "
                "authoritative booking.available_slots fact. This fictional provider confirms an "
                "approved proposal immediately."
            ),
            default_permission=PermissionOutcome.REQUIRE_APPROVAL,
            produces=(BOOKING_REFERENCE, BOOKING_STATUS, BOOKING_SLOT),
        ),
        Capability(
            action_type="request_payment",
            title="Request payment",
            description="Create a payment request for a confirmed booking.",
            parameters_model=RequestPaymentParameters,
            adapter=bindings["request_payment"],
            simulation_adapter=simulation_bindings["request_payment"],
            guidance=(
                "Use the confirmed booking_reference and the configured demo amount of 12500 "
                "minor units in NZD."
            ),
            default_permission=PermissionOutcome.REQUIRE_APPROVAL,
            produces=(
                PAYMENT_REFERENCE,
                PAYMENT_STATUS,
                PAYMENT_AMOUNT_MINOR,
                PAYMENT_CURRENCY,
            ),
        ),
        Capability(
            action_type="create_calendar_event",
            title="Create a calendar event",
            description="Add a paid, confirmed booking to the configured calendar.",
            parameters_model=CreateCalendarEventParameters,
            adapter=bindings["create_calendar_event"],
            simulation_adapter=simulation_bindings["create_calendar_event"],
            guidance=(
                "Use the confirmed booking_reference and booking.slot plus a nonblank "
                "customer-facing title. Propose this only after payment.status is completed."
            ),
            default_permission=PermissionOutcome.ALLOW,
            produces=(CALENDAR_REFERENCE, CALENDAR_STATUS),
        ),
    )
    journey = Journey(
        id="enquiry_to_booking",
        version="1",
        title="Enquiry to completed booking",
        description=(
            "Follow one customer enquiry through clarification, booking, payment, and calendar."
        ),
        goals=(
            "Establish what the customer needs",
            "Offer a valid booking within configured business rules",
            "Obtain exact approval where policy requires it",
            "Confirm payment and calendar state through authoritative providers",
        ),
        capabilities=tuple(capability.action_type for capability in capabilities),
        complete_when=(
            BOOKING_STATUS.equals("confirmed"),
            PAYMENT_STATUS.equals("completed"),
            CALENDAR_STATUS.equals("created"),
        ),
        decision_guidance=(
            "When a customer claim states a date and time that exactly matches an authoritative "
            "booking.available_slots value, treat it as the customer's slot selection.",
            "customer.email is the customer_id for propose_booking. A stated 60-minute "
            "appointment is sufficient service detail for this fictional provider.",
            "Once an exact selected slot is available, propose_booking for that exact slot.",
            "An operator request_revision instruction is binding for the next proposal.",
            "Human approval comes from a capability's permission; never emit a standalone human "
            "wake condition.",
            "Every waiting decision must include an allowed event or timer wake condition.",
            "After offering availability, wait for customer.email_received; after requesting "
            "payment, wait for payment.completed or payment.failed.",
            "Once propose_booking returns booking.status confirmed, request payment immediately.",
            "A conflict action result is definitive. Do not repeat the rejected action unchanged.",
            "Whenever you propose one or more actions, return status active, never completed. "
            "Return completed only on a later turn after every configured authoritative "
            "completion fact is satisfied.",
        ),
        outbound_action_types=("send_message",),
        reply_event_types=("customer.email_received",),
        decision_transformer=apply_fictional_transitions,
    )
    routes = (
        Route.start(
            "enquiry.created",
            journey=journey.id,
            title="Website enquiry received",
            description="Start one journey for a newly received customer enquiry.",
            provides=(CUSTOMER_EMAIL,),
        ),
        Route.wake(
            "customer.email_received",
            journey=journey.id,
            title="Customer replied",
            description="Wake when the customer replies to an outbound message.",
            provides=(CUSTOMER_LAST_MESSAGE,),
        ),
        Route.wake(
            "payment.completed",
            journey=journey.id,
            title="Payment completed",
            description="Wake on authoritative confirmation from the payment provider.",
            provides=(PAYMENT_REFERENCE, PAYMENT_STATUS, PAYMENT_AMOUNT_MINOR, PAYMENT_CURRENCY),
        ),
        Route.wake(
            "payment.failed",
            journey=journey.id,
            title="Payment failed",
            description="Wake when the payment provider reports a definitive failure.",
            provides=(PAYMENT_REFERENCE, PAYMENT_STATUS),
        ),
    )
    scenario = Scenario(
        id="happy_path",
        journey_id=journey.id,
        title="A customer books and pays",
        description="The normal journey from website enquiry to a paid calendar booking.",
        steps=(
            ScenarioStep.event(
                "enquiry.created",
                "A customer submits a website enquiry.",
                facts=(CUSTOMER_EMAIL.observed("customer@example.test"),),
                payload={
                    "email": "customer@example.test",
                    "message": "I would like a 60-minute appointment next week.",
                },
            ),
            ScenarioStep.action(
                "find_available_slots",
                "The agent checks authoritative availability.",
                parameters={"days": 7},
            ),
            ScenarioStep.action(
                "send_message",
                "The agent asks the customer to select an available time.",
                parameters={
                    "recipient": ScenarioValue.fact(CUSTOMER_EMAIL),
                    "body": "We have appointments available. Which time suits you?",
                },
                approve=True,
            ),
            ScenarioStep.wait_for_event(
                "customer.email_received", "The agent sleeps until the customer replies."
            ),
            ScenarioStep.event(
                "customer.email_received",
                "The customer selects the first available slot.",
                facts=(
                    CUSTOMER_LAST_MESSAGE.observed(
                        "The first time works for me.", kind=FactKind.CUSTOMER_CLAIM
                    ),
                ),
                payload={"content": "The first time works for me."},
            ),
            ScenarioStep.action(
                "propose_booking",
                "The booking is approved and confirmed.",
                parameters={
                    "customer_id": ScenarioValue.fact(CUSTOMER_EMAIL),
                    "slot": ScenarioValue.fact(AVAILABLE_SLOTS, 0),
                },
                approve=True,
            ),
            ScenarioStep.action(
                "request_payment",
                "The customer receives an approved payment request.",
                parameters={
                    "booking_reference": ScenarioValue.fact(BOOKING_REFERENCE),
                    "amount_minor": 12_500,
                    "currency": "NZD",
                },
                approve=True,
            ),
            ScenarioStep.wait_for_event(
                "payment.completed", "The agent sleeps until the payment provider responds."
            ),
            ScenarioStep.event(
                "payment.completed",
                "The provider confirms payment.",
                facts=(
                    PAYMENT_REFERENCE.observed(ScenarioValue.fact(PAYMENT_REFERENCE)),
                    PAYMENT_STATUS.observed("completed"),
                    PAYMENT_AMOUNT_MINOR.observed(12_500),
                    PAYMENT_CURRENCY.observed("NZD"),
                ),
                payload={
                    "payment_reference": ScenarioValue.fact(PAYMENT_REFERENCE),
                    "booking_reference": ScenarioValue.fact(BOOKING_REFERENCE),
                },
            ),
            ScenarioStep.action(
                "create_calendar_event",
                "The paid appointment is added to the calendar.",
                parameters={
                    "booking_reference": ScenarioValue.fact(BOOKING_REFERENCE),
                    "starts_at": ScenarioValue.fact(BOOKING_SLOT),
                    "title": "Customer appointment",
                },
            ),
            ScenarioStep.fact(BOOKING_STATUS, "confirmed", "The booking is authoritative."),
            ScenarioStep.fact(PAYMENT_STATUS, "completed", "Payment is authoritative."),
            ScenarioStep.fact(CALENDAR_STATUS, "created", "The calendar event is authoritative."),
            ScenarioStep.complete("The journey completes with no open work."),
        ),
        started_at=FICTIONAL_SCENARIO_STARTED_AT,
    )
    return Project(
        id="fictional_booking",
        version="0.1.0",
        title="Fictional booking business",
        description="A complete demonstration of a durable enquiry-to-booking relationship.",
        journeys=(journey,),
        routes=routes,
        capabilities=capabilities,
        facts=(
            CUSTOMER_EMAIL,
            CUSTOMER_LAST_MESSAGE,
            OUTBOUND_MESSAGE_REFERENCE,
            AVAILABLE_SLOTS,
            BOOKING_REFERENCE,
            BOOKING_STATUS,
            BOOKING_SLOT,
            PAYMENT_REFERENCE,
            PAYMENT_STATUS,
            PAYMENT_AMOUNT_MINOR,
            PAYMENT_CURRENCY,
            CALENDAR_REFERENCE,
            CALENDAR_STATUS,
        ),
        scenarios=(scenario,),
    )


def load_fictional_deployment(*, state: StubBusinessState | None = None) -> FictionalDeployment:
    try:
        return create_fictional_project(state=state).compile()
    except (ClientPackError, ValueError) as error:
        raise FictionalDeploymentError(f"invalid fictional client pack: {error}") from error
