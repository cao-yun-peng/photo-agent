"""Add durable embedding retry state to photos.

Revision ID: 20260815_0001
Revises: 20260808_0002
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0001"
down_revision: Union[str, None] = "20260808_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "photos",
        sa.Column(
            "embedding_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "photos",
        sa.Column("embedding_next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "photos",
        sa.Column("embedding_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "photos",
        sa.Column("embedding_last_error", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("photos", "embedding_last_error")
    op.drop_column("photos", "embedding_last_attempt_at")
    op.drop_column("photos", "embedding_next_retry_at")
    op.drop_column("photos", "embedding_retry_count")
