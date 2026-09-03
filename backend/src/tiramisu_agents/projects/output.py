"""Generate strict OpenAI output models from registered business capabilities."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    EventWakeCondition,
    MemoryUpdate,
    TimerWakeCondition,
)
from tiramisu_agents.core.contracts.processes import AgentTurnInput
from tiramisu_agents.projects.contracts import Capability, DecisionTransformer


class GeneratedActionProposalOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_action_key: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=1000)

    def to_action_proposal(self) -> ActionProposal:
        action_type = getattr(self, "action_type", None)
        parameters = getattr(self, "parameters", None)
        if not isinstance(action_type, str) or not isinstance(parameters, BaseModel):
            raise ValueError("generated action output is missing its typed capability")
        return ActionProposal(
            logical_action_key=self.logical_action_key,
            action_type=action_type,
            parameters=parameters.model_dump(mode="json"),
            rationale=self.rationale,
        )


class GeneratedAgentDecisionOutput(BaseModel):
    """Common conversion behavior for a generated project-specific output schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DecisionStatus
    actions: tuple[Any, ...] = ()
    wake_conditions: tuple[Any, ...] = ()
    memory_update: MemoryUpdate = Field(default_factory=MemoryUpdate)

    _decision_transformers: ClassVar[Mapping[str, DecisionTransformer]] = MappingProxyType({})

    @classmethod
    def configure_decision_transformers(
        cls, transformers: Mapping[str, DecisionTransformer]
    ) -> None:
        cls._decision_transformers = MappingProxyType(dict(transformers))

    @model_validator(mode="after")
    def waiting_requires_a_wake(self) -> "GeneratedAgentDecisionOutput":
        if self.status is DecisionStatus.WAITING and not self.wake_conditions:
            raise ValueError("a waiting decision requires at least one wake condition")
        return self

    def to_agent_decision(self, turn_input: AgentTurnInput) -> AgentDecision:
        decision = AgentDecision(
            based_on_event_ids=tuple(event.event_id for event in turn_input.events),
            based_on_review_command_ids=tuple(review.command_id for review in turn_input.reviews),
            based_on_action_attempt_ids=tuple(
                action_result.attempt_id for action_result in turn_input.action_results
            ),
            based_on_timer_ids=turn_input.timer_ids,
            status=self.status,
            actions=tuple(action.to_action_proposal() for action in self.actions),
            wake_conditions=self.wake_conditions,
            memory_update=self._trusted_memory_update(turn_input),
        )
        transformer = self._decision_transformers.get(turn_input.process.process_type)
        return transformer(decision, turn_input) if transformer is not None else decision

    def _trusted_memory_update(self, turn_input: AgentTurnInput) -> MemoryUpdate:
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
        supplied_provenance_was_rewritten = (
            summary_event_ids != self.memory_update.summary_source_event_ids
            or summary_review_ids != self.memory_update.summary_source_review_command_ids
            or summary_action_ids != self.memory_update.summary_source_action_attempt_ids
            or summary_timer_ids != self.memory_update.summary_source_timer_ids
        )
        if self.memory_update.summary is not None and (
            supplied_provenance_was_rewritten
            or not any(
                (summary_event_ids, summary_review_ids, summary_action_ids, summary_timer_ids)
            )
        ):
            return MemoryUpdate(open_commitments=self.memory_update.open_commitments)
        return MemoryUpdate(
            summary=self.memory_update.summary,
            summary_source_event_ids=summary_event_ids,
            summary_source_review_command_ids=summary_review_ids,
            summary_source_action_attempt_ids=summary_action_ids,
            summary_source_timer_ids=summary_timer_ids,
            open_commitments=self.memory_update.open_commitments,
        )


def _model_name(value: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^a-zA-Z0-9]+", value) if part)


def _literal(values: tuple[str, ...]) -> Any:
    return Literal.__getitem__(values)  # type: ignore[attr-defined]


def generate_agent_decision_output_type(
    *,
    project_id: str,
    project_version: str,
    capabilities: tuple[Capability, ...],
    wake_event_types: tuple[str, ...],
    decision_transformers: Mapping[str, DecisionTransformer],
) -> type[GeneratedAgentDecisionOutput]:
    """Build a strict discriminated action union for one immutable project version."""

    proposal_types: list[type[GeneratedActionProposalOutput]] = []
    for capability in capabilities:
        proposal_type = create_model(
            f"{_model_name(capability.action_type)}ProposalOutput",
            __base__=GeneratedActionProposalOutput,
            __module__="tiramisu_agents.projects.generated",
            action_type=(_literal((capability.action_type,)), ...),
            parameters=(capability.parameters_model, ...),
        )
        proposal_types.append(proposal_type)

    actions_type: Any = tuple[()]
    if proposal_types:
        proposal_union: Any = proposal_types[0]
        for proposal_type in proposal_types[1:]:
            proposal_union |= proposal_type
        if len(proposal_types) > 1:
            proposal_union = Annotated[proposal_union, Field(discriminator="action_type")]
        actions_type = tuple[proposal_union, ...]

    wake_types: list[Any] = [TimerWakeCondition]
    if wake_event_types:
        event_wake_type = create_model(
            "ProjectEventWakeCondition",
            __base__=EventWakeCondition,
            __module__="tiramisu_agents.projects.generated",
            event_type=(_literal(wake_event_types), ...),
        )
        wake_types.insert(0, event_wake_type)
    wake_union: Any = wake_types[0]
    for wake_type in wake_types[1:]:
        wake_union |= wake_type
    if len(wake_types) > 1:
        wake_union = Annotated[wake_union, Field(discriminator="type")]

    output_type = create_model(
        f"{_model_name(project_id)}AgentDecisionOutputV{_model_name(project_version)}",
        __base__=GeneratedAgentDecisionOutput,
        __module__="tiramisu_agents.projects.generated",
        actions=(actions_type, ()),
        wake_conditions=(tuple[wake_union, ...], ()),
    )
    output_type.configure_decision_transformers(decision_transformers)
    return output_type
