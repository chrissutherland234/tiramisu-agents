"""Pin client-pack and process-definition compatibility on process instances.

Revision ID: 20260901_12
Revises: 20260831_11
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_12"
down_revision: str | None = "20260831_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNKNOWN_FINGERPRINT = "0" * 64


def upgrade() -> None:
    op.add_column(
        "process_instances",
        sa.Column("client_pack_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "process_instances",
        sa.Column("process_definition_fingerprint", sa.String(length=64), nullable=True),
    )
    # Historical rows do not contain enough information to reconstruct an exact
    # client-pack composition. A sentinel makes them explicit and fail-closed;
    # they require a future audited migration before external work may resume.
    op.execute(
        sa.text(
            "UPDATE process_instances "
            "SET client_pack_fingerprint = :unknown, "
            "process_definition_fingerprint = :unknown"
        ).bindparams(unknown=_UNKNOWN_FINGERPRINT)
    )
    op.alter_column("process_instances", "client_pack_fingerprint", nullable=False)
    op.alter_column("process_instances", "process_definition_fingerprint", nullable=False)
    op.create_check_constraint(
        "client_pack_fingerprint_sha256",
        "process_instances",
        "client_pack_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "process_definition_fingerprint_sha256",
        "process_instances",
        "process_definition_fingerprint ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_process_instances_process_definition_fingerprint_sha256"),
        "process_instances",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_process_instances_client_pack_fingerprint_sha256"),
        "process_instances",
        type_="check",
    )
    op.drop_column("process_instances", "process_definition_fingerprint")
    op.drop_column("process_instances", "client_pack_fingerprint")
