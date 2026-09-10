"""Generation cancellation, leases and bounded iterations."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "20260906_0003"
down_revision = "20260906_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "generations",
        sa.Column(
            "progress_stage",
            sa.String(32),
            nullable=False,
            server_default="awaiting_confirmation",
        ),
    )
    op.add_column(
        "generations", sa.Column("lease_token", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "generations", sa.Column("lease_expires_at", sa.DateTime(timezone=True))
    )
    for name in ("parent_generation_id", "root_generation_id"):
        op.add_column(
            "generations",
            sa.Column(
                name,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("generations.id", ondelete="RESTRICT"),
            ),
        )
    op.add_column(
        "generations",
        sa.Column("iteration_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_generations_root_generation_id", "generations", ["root_generation_id"]
    )
    op.execute(
        "UPDATE generations SET progress_stage = CASE WHEN status='processing' THEN 'generating' ELSE status END"
    )


def downgrade():
    op.execute(
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM generations WHERE parent_generation_id IS NOT NULL OR lease_token IS NOT NULL OR status IN ('cancelled','cancel_requested','outcome_unknown','expired')) THEN RAISE EXCEPTION 'P5 task data prevents downgrade'; END IF; END $$"""
    )
    op.drop_index("ix_generations_root_generation_id", table_name="generations")
    for name in (
        "iteration_index",
        "root_generation_id",
        "parent_generation_id",
        "lease_expires_at",
        "lease_token",
        "progress_stage",
    ):
        op.drop_column("generations", name)
