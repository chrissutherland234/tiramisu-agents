"""Integration boundary for one bounded, proposal-only reasoning turn."""

from dataclasses import dataclass
from typing import Protocol

from tiramisu_agents.core.contracts.decisions import AgentDecision
from tiramisu_agents.core.contracts.processes import AgentTurnInput


@dataclass(frozen=True, slots=True)
class ProposalCorrection:
    """Controlled feedback for replacing one semantically invalid proposal."""

    correction_attempt: int
    rejected_decision: AgentDecision
    validation_error: str

    def __post_init__(self) -> None:
        if self.correction_attempt < 1:
            raise ValueError("correction_attempt must be positive")
        if not self.validation_error:
            raise ValueError("validation_error cannot be empty")


class AgentTurnRunner(Protocol):
    async def run_turn(
        self,
        turn_input: AgentTurnInput,
        *,
        correction: ProposalCorrection | None = None,
    ) -> AgentDecision:
        """Produce a typed proposal without executing mutating provider actions."""
        ...
