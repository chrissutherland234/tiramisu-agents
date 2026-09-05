"""A deterministic agent-turn runner for kernel and workflow scenarios."""

from collections import deque
from collections.abc import Callable, Iterable

from tiramisu_agents.agents.runner import ModelTurnOutcome, ProposalCorrection
from tiramisu_agents.budgets.policy import ModelUsage
from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.contracts.processes import AgentTurnInput


class ScriptedAgent:
    def __init__(
        self,
        decisions: Iterable[AgentDecision | Callable[[AgentTurnInput], AgentDecision]],
        *,
        model: str = "gpt-4o-mini",
        usages: Iterable[ModelUsage] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("a scripted agent requires a model name for usage records")
        self._decisions = deque(decisions)
        self._model = model
        self._usages = deque(usages or ())
        self.turn_inputs: list[AgentTurnInput] = []
        self.corrections: list[ProposalCorrection | None] = []

    async def run_turn(
        self,
        turn_input: AgentTurnInput,
        *,
        correction: ProposalCorrection | None = None,
    ) -> ModelTurnOutcome:
        self.turn_inputs.append(turn_input)
        self.corrections.append(correction)
        if not self._decisions:
            raise RuntimeError("scripted agent has no decision for this turn")
        scripted = self._decisions.popleft()
        usage = self._usages.popleft() if self._usages else ModelUsage()
        return ModelTurnOutcome(
            decision=scripted(turn_input) if callable(scripted) else scripted,
            usage=usage,
            model=self._model,
        )
