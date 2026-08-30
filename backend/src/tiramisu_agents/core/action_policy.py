"""Deterministic action permission classification."""

from dataclasses import dataclass

from tiramisu_agents.core.contracts.actions import PermissionOutcome
from tiramisu_agents.core.contracts.decisions import ActionProposal


@dataclass(frozen=True, slots=True)
class ActionPolicyDecision:
    outcome: PermissionOutcome
    policy_version: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConfiguredActionPolicy:
    permissions: dict[str, PermissionOutcome]
    version: str

    def evaluate(self, action: ActionProposal) -> ActionPolicyDecision:
        outcome = self.permissions.get(action.action_type, PermissionOutcome.DENY)
        reason = (
            f"action '{action.action_type}' is configured as {outcome.value} "
            f"by policy {self.version}"
        )
        return ActionPolicyDecision(outcome=outcome, policy_version=self.version, reason=reason)
