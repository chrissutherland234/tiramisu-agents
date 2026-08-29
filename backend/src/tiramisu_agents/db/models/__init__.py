"""Persistent orchestration models.

Importing this module registers every model on ``Base.metadata`` for Alembic.
"""

from tiramisu_agents.db.models.events import EventInbox, ExternalCorrelation, OutboxMessage
from tiramisu_agents.db.models.processes import ProcessInstance
from tiramisu_agents.db.models.tenancy import Tenant

__all__ = [
    "EventInbox",
    "ExternalCorrelation",
    "OutboxMessage",
    "ProcessInstance",
    "Tenant",
]
