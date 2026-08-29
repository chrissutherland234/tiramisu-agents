"""Integration boundary for one bounded, proposal-only reasoning turn."""

from typing import Protocol

from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.contracts.processes import AgentTurnInput


class AgentTurnRunner(Protocol):
    async def run_turn(self, turn_input: AgentTurnInput) -> AgentDecision:
        """Produce a typed proposal without executing mutating provider actions."""
        ...
