"""Strict structured output contract for the bundled fictional process."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    MemoryUpdate,
    WakeCondition,
)
from tiramisu_agents.core.contracts.processes import AgentTurnInput


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
    amount_minor: int = Field(ge=1)
    currency: str = Field(min_length=1, max_length=16)


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


class FictionalAgentDecisionOutput(BaseModel):
    """The fictional pack's action types and exact provider-facing parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DecisionStatus
    actions: tuple[FictionalActionProposalOutput, ...] = ()
    wake_conditions: tuple[WakeCondition, ...] = ()
    memory_update: MemoryUpdate = Field(default_factory=MemoryUpdate)

    def to_agent_decision(self, turn_input: AgentTurnInput) -> AgentDecision:
        return AgentDecision(
            based_on_event_ids=tuple(event.event_id for event in turn_input.events),
            based_on_review_command_ids=tuple(review.command_id for review in turn_input.reviews),
            based_on_action_attempt_ids=tuple(
                action_result.attempt_id for action_result in turn_input.action_results
            ),
            based_on_timer_ids=turn_input.timer_ids,
            status=self.status,
            actions=tuple(action.to_action_proposal() for action in self.actions),
            wake_conditions=self.wake_conditions,
            memory_update=self.memory_update,
        )
