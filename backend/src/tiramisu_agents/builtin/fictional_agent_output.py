"""Typed capability parameters and deterministic transitions for the fictional project."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tiramisu_agents.core.contracts.actions import ActionAttemptStatus
from tiramisu_agents.core.contracts.decisions import ActionProposal, AgentDecision, DecisionStatus
from tiramisu_agents.core.contracts.processes import AgentTurnInput

FICTIONAL_DEFAULT_PAYMENT_AMOUNT_MINOR = 12_500
FICTIONAL_DEFAULT_PAYMENT_CURRENCY = "NZD"


class _FictionalActionParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _NonblankStringParameters(_FictionalActionParameters):
    @field_validator("*")
    @classmethod
    def require_nonblank_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("string parameters must be nonblank")
        return value


class SendMessageParameters(_NonblankStringParameters):
    recipient: str = Field(min_length=1, max_length=320)
    body: str = Field(min_length=1, max_length=10_000)


class FindAvailableSlotsParameters(_FictionalActionParameters):
    days: int = Field(ge=1, le=365)


class ProposeBookingParameters(_NonblankStringParameters):
    customer_id: str = Field(min_length=1, max_length=255)
    slot: str = Field(min_length=1, max_length=255)


class RequestPaymentParameters(_NonblankStringParameters):
    booking_reference: str = Field(min_length=1, max_length=255)
    amount_minor: Literal[12_500]
    currency: Literal["NZD"]


class CreateCalendarEventParameters(_NonblankStringParameters):
    booking_reference: str = Field(min_length=1, max_length=255)
    starts_at: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)


def apply_fictional_transitions(
    decision: AgentDecision,
    turn_input: AgentTurnInput,
) -> AgentDecision:
    """Enforce the demo's immediate confirmed-booking to payment transition."""

    confirmed_booking_reference = _confirmed_booking_reference(turn_input)
    if confirmed_booking_reference is None or any(
        action.action_type == "request_payment" for action in decision.actions
    ):
        return decision
    payment = ActionProposal(
        logical_action_key="request-payment-after-booking",
        action_type="request_payment",
        parameters={
            "booking_reference": confirmed_booking_reference,
            "amount_minor": FICTIONAL_DEFAULT_PAYMENT_AMOUNT_MINOR,
            "currency": FICTIONAL_DEFAULT_PAYMENT_CURRENCY,
        },
        rationale=("The fictional booking is confirmed; request the configured demo payment next."),
    )
    return decision.model_copy(
        update={
            "actions": (*decision.actions, payment),
            "status": (
                DecisionStatus.ACTIVE
                if decision.status is DecisionStatus.COMPLETED
                else decision.status
            ),
        }
    )


def _confirmed_booking_reference(turn_input: AgentTurnInput) -> str | None:
    for action_result in reversed(turn_input.action_results):
        if (
            action_result.action_type != "propose_booking"
            or action_result.status is not ActionAttemptStatus.SUCCEEDED
        ):
            continue
        facts = {fact.key: fact.value for fact in action_result.facts}
        if facts.get("booking.status") != "confirmed":
            continue
        reference = facts.get("booking.reference")
        if isinstance(reference, str) and reference.strip():
            return reference
        result = action_result.result
        if isinstance(result, dict):
            value = result.get("booking_reference")
            if isinstance(value, str) and value.strip():
                return value
    return None
