"""One durable process instance per client business thing."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ProcessInstance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "process_instances"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'waiting', 'review', 'completed', 'cancelled', 'failed')",
            name="status_valid",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_process_instances_tenant_id_id"),
        UniqueConstraint("tenant_id", "workflow_id", name="uq_process_instances_tenant_workflow"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    process_type: Mapped[str] = mapped_column(String(100), nullable=False)
    definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extension_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default="active", nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    current_run_id: Mapped[str | None] = mapped_column(String(255))
