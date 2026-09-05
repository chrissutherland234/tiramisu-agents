"""Tenant-scoped circuit breakers with append-only audited transitions."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class CircuitBreaker(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One breaker transition; the latest row per scope/target wins.

    Rows are append-only: the runtime role receives SELECT and INSERT but no
    UPDATE. Tripping an already-tripped breaker (or resetting a closed one)
    is a conflict, so history never fills with no-op transitions.
    """

    __tablename__ = "circuit_breakers"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('model_calls', 'outbound_messages', 'capability', 'all')",
            name="scope_valid",
        ),
        CheckConstraint(
            "(scope = 'capability' AND target <> '') OR (scope <> 'capability' AND target = '')",
            name="scope_target_valid",
        ),
        Index(
            "ix_circuit_breakers_latest",
            "tenant_id",
            "scope",
            "target",
            "created_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    tripped: Mapped[bool] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
