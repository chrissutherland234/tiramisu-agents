"""Deterministic action permission classification."""

from dataclasses import dataclass

from tiramisu_agents.core.contracts.actions import ActionRequestStatus, PermissionOutcome
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


def initial_action_request_status(outcome: PermissionOutcome) -> ActionRequestStatus:
    """Map a policy outcome to the initial durable action state."""

    return {
        PermissionOutcome.ALLOW: ActionRequestStatus.ALLOWED,
        PermissionOutcome.DENY: ActionRequestStatus.DENIED,
        PermissionOutcome.REQUIRE_APPROVAL: ActionRequestStatus.PENDING_APPROVAL,
    }[outcome]
