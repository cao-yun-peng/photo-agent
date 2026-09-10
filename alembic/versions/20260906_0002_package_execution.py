"""Frozen package execution inputs.
Revision ID: 20260906_0002
Revises: 20260906_0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "20260906_0002"
down_revision = "20260906_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "generations",
        sa.Column("execution_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "generations", sa.Column("execution_digest", sa.String(64), nullable=True)
    )
    op.add_column(
        "generations", sa.Column("verification", postgresql.JSONB(), nullable=True)
    )
    op.create_table(
        "generation_inputs",
        sa.Column(
            "generation_id",
            sa.UUID(),
            sa.ForeignKey("generations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM generations WHERE execution_snapshot IS NOT NULL) THEN
            RAISE EXCEPTION 'Archive and explicitly remove package executions before downgrade';
        END IF;
    END $$;""")
    op.drop_table("generation_inputs")
    op.drop_column("generations", "verification")
    op.drop_column("generations", "execution_digest")
    op.drop_column("generations", "execution_snapshot")
