"""Add Agent v2 generation confirmation, idempotency and quota reservation.

Revision ID: 20260822_0002
Revises: 20260822_0001
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0002"
down_revision: Union[str, None] = "20260822_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "generations",
        "status",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.add_column(
        "rate_limits",
        sa.Column("gen_reserved", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "generations", sa.Column("estimated_cost_yuan", sa.Numeric(8, 4), nullable=True)
    )
    op.add_column(
        "generations", sa.Column("idempotency_key", sa.String(128), nullable=True)
    )
    op.add_column(
        "generations", sa.Column("confirmation_token", sa.UUID(), nullable=True)
    )
    op.add_column(
        "generations",
        sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "generations",
        sa.Column(
            "quota_reserved", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "generations", sa.Column("quota_reserved_day", sa.Date(), nullable=True)
    )
    op.add_column(
        "generations",
        sa.Column(
            "enqueue_status", sa.String(16), nullable=False, server_default="not_queued"
        ),
    )
    op.add_column(
        "generations",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "generations", sa.Column("last_error_code", sa.String(64), nullable=True)
    )
    op.create_unique_constraint(
        "uq_generations_user_idempotency",
        "generations",
        ["user_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_generations_confirmation_token", "generations", ["confirmation_token"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_generations_confirmation_token", "generations", type_="unique"
    )
    op.drop_constraint("uq_generations_user_idempotency", "generations", type_="unique")
    op.drop_column("generations", "last_error_code")
    op.drop_column("generations", "attempt_count")
    op.drop_column("generations", "enqueue_status")
    op.drop_column("generations", "quota_reserved_day")
    op.drop_column("generations", "quota_reserved")
    op.drop_column("generations", "confirmation_expires_at")
    op.drop_column("generations", "confirmation_token")
    op.drop_column("generations", "idempotency_key")
    op.drop_column("generations", "estimated_cost_yuan")
    op.drop_column("rate_limits", "gen_reserved")
    op.execute(
        "UPDATE generations SET status = 'pending' "
        "WHERE status = 'awaiting_confirmation'"
    )
    op.execute(
        "UPDATE generations SET status = 'failed' "
        "WHERE status IN ('retryable_failed', 'queue_failed')"
    )
    op.alter_column(
        "generations",
        "status",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
    )
