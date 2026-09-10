"""Durable planning claims and provider call ledger."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
from alembic import op

revision = "20260907_0001"
down_revision = "20260906_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "planning_operations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "request_key", name="uq_planning_owner_key"),
    )
    op.create_table(
        "provider_calls",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "planning_id",
            UUID(as_uuid=True),
            sa.ForeignKey("planning_operations.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "generation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("generations.id", ondelete="SET NULL"),
        ),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_provider_calls_user_id", "provider_calls", ["user_id"])


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM planning_operations) OR EXISTS (SELECT 1 FROM provider_calls)"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "Provider evidence exists; archive and reconcile before downgrade"
        )
    op.drop_table("provider_calls")
    op.drop_table("planning_operations")
