"""Persistent orchestration models.

Importing this module registers every model on ``Base.metadata`` for Alembic.
"""

from tiramisu_agents.db.models.actions import (
    ActionAttempt,
    ActionPolicyRecord,
    ActionReconciliationDecision,
    ActionRequest,
    ActionRevision,
    ApprovalRequest,
)
from tiramisu_agents.db.models.breakers import CircuitBreaker
from tiramisu_agents.db.models.events import (
    EventInbox,
    EventResolutionCommand,
    ExternalCorrelation,
    OutboxMessage,
    OutboxRecoveryCommand,
)
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import (
    Tenant,
    TenantCredential,
    TenantDeploymentEvent,
    TenantSafetyEvent,
)
from tiramisu_agents.db.models.usage import ModelUsageLedger

__all__ = [
    "ActionAttempt",
    "ActionPolicyRecord",
    "ActionReconciliationDecision",
    "ActionRequest",
    "ActionRevision",
    "ApprovalRequest",
    "ApprovalDecision",
    "CircuitBreaker",
    "EventInbox",
    "EventResolutionCommand",
    "ExternalCorrelation",
    "ModelUsageLedger",
    "OutboxMessage",
    "OutboxRecoveryCommand",
    "ProcessInstance",
    "ProcessIntervention",
    "ProcessControlCommand",
    "ProcessStateRevision",
    "ReviewMessage",
    "ReviewThread",
    "Tenant",
    "TenantCredential",
    "TenantDeploymentEvent",
    "TenantSafetyEvent",
]
