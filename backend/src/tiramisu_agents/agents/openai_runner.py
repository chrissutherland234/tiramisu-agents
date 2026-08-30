"""Proposal-only OpenAI Agents SDK adapter."""

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from uuid import UUID

from agents import Agent, RunConfig, Runner
from pydantic import BaseModel, ConfigDict, Field

from tiramisu_agents.core.contracts.decisions import (
    ActionProposal,
    AgentDecision,
    DecisionStatus,
    MemoryUpdate,
    WakeCondition,
)
from tiramisu_agents.core.contracts.processes import AgentTurnInput


class AgentsSDKResult(Protocol):
    @property
    def final_output(self) -> Any: ...


AgentsSDKExecutor = Callable[[Agent[None], str, int, RunConfig], Awaitable[AgentsSDKResult]]


class ActionProposalOutput(BaseModel):
    """Strict-schema transport for provider-specific action parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_action_key: str = Field(min_length=1, max_length=200)
    action_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    parameters_json: str = Field(
        description="A JSON object containing only the proposed action parameters."
    )
    rationale: str = Field(min_length=1, max_length=1000)

    def to_action_proposal(self) -> ActionProposal:
        decoded: Any = json.loads(self.parameters_json)
        if not isinstance(decoded, dict):
            raise ValueError("action parameters_json must decode to an object")
        return ActionProposal(
            logical_action_key=self.logical_action_key,
            action_type=self.action_type,
            parameters=cast(dict[str, Any], decoded),
            rationale=self.rationale,
        )


class AgentDecisionOutput(BaseModel):
    """Strict Agents SDK output converted into the richer kernel contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    based_on_event_ids: tuple[str, ...]
    based_on_review_command_ids: tuple[str, ...] = ()
    status: DecisionStatus
    actions: tuple[ActionProposalOutput, ...] = ()
    wake_conditions: tuple[WakeCondition, ...] = ()
    memory_update: MemoryUpdate = Field(default_factory=MemoryUpdate)

    def to_agent_decision(self) -> AgentDecision:
        return AgentDecision(
            based_on_event_ids=tuple(UUID(value) for value in self.based_on_event_ids),
            based_on_review_command_ids=tuple(
                UUID(value) for value in self.based_on_review_command_ids
            ),
            status=self.status,
            actions=tuple(action.to_action_proposal() for action in self.actions),
            wake_conditions=self.wake_conditions,
            memory_update=self.memory_update,
        )


async def _run_agents_sdk(
    agent: Agent[None], prompt: str, max_turns: int, run_config: RunConfig
) -> AgentsSDKResult:
    return await Runner.run(
        agent,
        prompt,
        max_turns=max_turns,
        run_config=run_config,
    )


class OpenAIAgentsTurnRunner:
    """Run one structured model turn with no executable tools or handoffs."""

    def __init__(
        self,
        *,
        model: str,
        max_turns: int = 1,
        tracing_disabled: bool = True,
        executor: AgentsSDKExecutor = _run_agents_sdk,
    ) -> None:
        if not model.strip():
            raise ValueError("an explicit OpenAI model is required")
        if max_turns != 1:
            raise ValueError("the proposal-only runner currently permits exactly one model turn")
        self._model = model
        self._max_turns = max_turns
        self._tracing_disabled = tracing_disabled
        self._executor = executor

    async def run_turn(self, turn_input: AgentTurnInput) -> AgentDecision:
        agent = Agent[None](
            name="Tiramisu proposal agent",
            instructions=(
                "You produce a typed proposal for one bounded business-process turn. "
                "You cannot execute actions. Do not invent action or event types.\n\n"
                f"{turn_input.instructions}"
            ),
            model=self._model,
            output_type=AgentDecisionOutput,
            tools=[],
            handoffs=[],
        )
        run_config = RunConfig(
            tracing_disabled=self._tracing_disabled,
            trace_include_sensitive_data=False,
            workflow_name="Tiramisu proposal turn",
        )
        result = await self._executor(
            agent,
            self._render_prompt(turn_input),
            self._max_turns,
            run_config,
        )
        output = AgentDecisionOutput.model_validate(result.final_output)
        return output.to_agent_decision()

    @staticmethod
    def _render_prompt(turn_input: AgentTurnInput) -> str:
        payload = {
            "turn_id": str(turn_input.turn_id),
            "process": turn_input.process.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in turn_input.events],
            "reviews": [review.model_dump(mode="json") for review in turn_input.reviews],
        }
        return (
            "Review this bounded process snapshot and its newly received events. "
            "Return the next AgentDecision.\n"
            f"{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
        )
