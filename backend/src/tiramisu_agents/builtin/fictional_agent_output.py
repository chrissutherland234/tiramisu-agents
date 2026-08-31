"""Strict structured output contract for the bundled fictional process."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tiramisu_agents.core.contracts.actions import ActionAttemptStatus
from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    MemoryUpdate,
    TimerWakeCondition,
)
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


class _FictionalActionProposalOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_action_key: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=1000)

    def to_action_proposal(self) -> ActionProposal:
        action_type = getattr(self, "action_type", None)
        parameters = getattr(self, "parameters", None)
        if not isinstance(action_type, str) or not isinstance(parameters, BaseModel):
            raise ValueError("fictional action output is missing its action type")
        return ActionProposal(
            logical_action_key=self.logical_action_key,
            action_type=action_type,
            parameters=parameters.model_dump(mode="json"),
            rationale=self.rationale,
        )


class SendMessageProposalOutput(_FictionalActionProposalOutput):
    action_type: Literal["send_message"]
    parameters: SendMessageParameters


class FindAvailableSlotsProposalOutput(_FictionalActionProposalOutput):
    action_type: Literal["find_available_slots"]
    parameters: FindAvailableSlotsParameters


class ProposeBookingProposalOutput(_FictionalActionProposalOutput):
    action_type: Literal["propose_booking"]
    parameters: ProposeBookingParameters


class RequestPaymentProposalOutput(_FictionalActionProposalOutput):
    action_type: Literal["request_payment"]
    parameters: RequestPaymentParameters


class CreateCalendarEventProposalOutput(_FictionalActionProposalOutput):
    action_type: Literal["create_calendar_event"]
    parameters: CreateCalendarEventParameters


FictionalActionProposalOutput = Annotated[
    SendMessageProposalOutput
    | FindAvailableSlotsProposalOutput
    | ProposeBookingProposalOutput
    | RequestPaymentProposalOutput
    | CreateCalendarEventProposalOutput,
    Field(discriminator="action_type"),
]

# Human approval is a consequence of an approval-gated action in this pack,
# not a free-standing model wake condition.  Keeping it out of the structured
# output schema prevents the model from producing an orphan approval wake.
FictionalWakeCondition = Annotated[
    EventWakeCondition | TimerWakeCondition,
    Field(discriminator="type"),
]


class FictionalAgentDecisionOutput(BaseModel):
    """The fictional pack's action types and exact provider-facing parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DecisionStatus
    actions: tuple[FictionalActionProposalOutput, ...] = ()
    wake_conditions: tuple[FictionalWakeCondition, ...] = ()
    memory_update: MemoryUpdate = Field(default_factory=MemoryUpdate)

    @model_validator(mode="after")
    def waiting_requires_a_wake(self) -> "FictionalAgentDecisionOutput":
        if self.status is DecisionStatus.WAITING and not self.wake_conditions:
            raise ValueError("a waiting fictional decision requires at least one wake condition")
        return self

    def to_agent_decision(self, turn_input: AgentTurnInput) -> AgentDecision:
        actions = tuple(action.to_action_proposal() for action in self.actions)
        confirmed_booking_reference = self._confirmed_booking_reference(turn_input)
        if confirmed_booking_reference is not None and not any(
            action.action_type == "request_payment" for action in actions
        ):
            # The fictional adapter confirms an approved proposal immediately.
            # Ensure a model no-op cannot park the process behind a customer
            # wake when the next deterministic step is the configured demo
            # payment request.
            actions += (
                ActionProposal(
                    logical_action_key="request-payment-after-booking",
                    action_type="request_payment",
                    parameters={
                        "booking_reference": confirmed_booking_reference,
                        "amount_minor": FICTIONAL_DEFAULT_PAYMENT_AMOUNT_MINOR,
                        "currency": FICTIONAL_DEFAULT_PAYMENT_CURRENCY,
                    },
                    rationale=(
                        "The fictional booking is confirmed; request the configured "
                        "demo payment next."
                    ),
                ),
            )
        memory_update = self._trusted_memory_update(turn_input)
        return AgentDecision(
            based_on_event_ids=tuple(event.event_id for event in turn_input.events),
            based_on_review_command_ids=tuple(review.command_id for review in turn_input.reviews),
            based_on_action_attempt_ids=tuple(
                action_result.attempt_id for action_result in turn_input.action_results
            ),
            based_on_timer_ids=turn_input.timer_ids,
            status=self.status,
            actions=actions,
            wake_conditions=self.wake_conditions,
            memory_update=memory_update,
        )

    def _trusted_memory_update(self, turn_input: AgentTurnInput) -> MemoryUpdate:
        """Keep only memory provenance IDs present in this bounded turn."""
        event_ids = frozenset(event.event_id for event in turn_input.events)
        review_ids = frozenset(review.command_id for review in turn_input.reviews)
        action_ids = frozenset(result.attempt_id for result in turn_input.action_results)
        timer_ids = frozenset(turn_input.timer_ids)
        summary_event_ids = tuple(
            value for value in self.memory_update.summary_source_event_ids if value in event_ids
        )
        summary_review_ids = tuple(
            value
            for value in self.memory_update.summary_source_review_command_ids
            if value in review_ids
        )
        summary_action_ids = tuple(
            value
            for value in self.memory_update.summary_source_action_attempt_ids
            if value in action_ids
        )
        summary_timer_ids = tuple(
            value for value in self.memory_update.summary_source_timer_ids if value in timer_ids
        )
        if self.memory_update.summary is not None and not any(
            (summary_event_ids, summary_review_ids, summary_action_ids, summary_timer_ids)
        ):
            # An ungrounded model summary is less useful than no summary, and
            # must never be allowed to smuggle historical provenance forward.
            return MemoryUpdate(open_commitments=self.memory_update.open_commitments)
        return MemoryUpdate(
            summary=self.memory_update.summary,
            summary_source_event_ids=summary_event_ids,
            summary_source_review_command_ids=summary_review_ids,
            summary_source_action_attempt_ids=summary_action_ids,
            summary_source_timer_ids=summary_timer_ids,
            open_commitments=self.memory_update.open_commitments,
        )

    @staticmethod
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
