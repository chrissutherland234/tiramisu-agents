"""Durable per-attempt model token/cost ledger."""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from tiramisu_agents.db.base import Base
from tiramisu_agents.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ModelUsageLedger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable recorded model spend for one agent-turn attempt.

    Rows are append-only: the runtime role receives SELECT and INSERT but no
    UPDATE, and later price-table changes never rewrite recorded cost.
    """

    __tablename__ = "model_usage_ledger"
    __table_args__ = (
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("cost_micros >= 0", name="cost_micros_nonnegative"),
        CheckConstraint("price_table_version >= 1", name="price_table_version_positive"),
        ForeignKeyConstraint(
            ["tenant_id", "process_instance_id"],
            ["process_instances.tenant_id", "process_instances.id"],
            name="fk_model_usage_ledger_process_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "process_instance_id",
            "agent_turn_id",
            "execution_id",
            "attempt_number",
            name="uq_model_usage_turn_attempt",
        ),
        Index(
            "ix_model_usage_spend",
            "tenant_id",
            "process_instance_id",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    process_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    agent_turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("'00000000-0000-0000-0000-000000000000'::uuid"),
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_table_version: Mapped[int] = mapped_column(Integer, nullable=False)
