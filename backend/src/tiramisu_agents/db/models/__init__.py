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
from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import (
    ProcessControlCommand,
    ProcessInstance,
    ProcessIntervention,
    ProcessStateRevision,
)
from tiramisu_agents.db.models.reviews import ApprovalDecision, ReviewMessage, ReviewThread
from tiramisu_agents.db.models.tenancy import Tenant, TenantCredential, TenantSafetyEvent

__all__ = [
    "ActionAttempt",
    "ActionPolicyRecord",
    "ActionReconciliationDecision",
    "ActionRequest",
    "ActionRevision",
    "ApprovalRequest",
    "ApprovalDecision",
    "EventInbox",
    "ExternalCorrelation",
    "OutboxMessage",
    "ProcessInstance",
    "ProcessIntervention",
    "ProcessControlCommand",
    "ProcessStateRevision",
    "ReviewMessage",
    "ReviewThread",
    "Tenant",
    "TenantCredential",
    "TenantSafetyEvent",
]
