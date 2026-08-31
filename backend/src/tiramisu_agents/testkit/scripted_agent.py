"""A deterministic agent-turn runner for kernel and workflow scenarios."""

from collections import deque
from collections.abc import Callable, Iterable

from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.contracts.processes import AgentTurnInput


class ScriptedAgent:
    def __init__(
        self,
        decisions: Iterable[AgentDecision | Callable[[AgentTurnInput], AgentDecision]],
    ) -> None:
        self._decisions = deque(decisions)
        self.turn_inputs: list[AgentTurnInput] = []

    async def run_turn(self, turn_input: AgentTurnInput) -> AgentDecision:
        self.turn_inputs.append(turn_input)
        if not self._decisions:
            raise RuntimeError("scripted agent has no decision for this turn")
        scripted = self._decisions.popleft()
        return scripted(turn_input) if callable(scripted) else scripted
