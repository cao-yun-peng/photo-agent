"""Add task ownership and durable photo recovery.
Revision ID: 20260905_0001
Revises: 20260822_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20260905_0001"
down_revision = "20260822_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("agent_run_token", sa.UUID(), nullable=True))
    op.add_column("photos", sa.Column("processing_token", sa.UUID(), nullable=True))
    op.add_column(
        "photos",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "photos",
        sa.Column("processing_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "photos",
        sa.Column(
            "processing_dispatch_until", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "photos",
        sa.Column(
            "processing_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "photos",
        sa.Column("processing_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_photos_processing_lease_until", "photos", ["processing_lease_until"]
    )
    op.create_index("ix_photos_processing_retry_at", "photos", ["processing_retry_at"])
    # Existing processing rows have no owner. Recovery treats NULL leases as expired.


def downgrade():
    op.drop_index("ix_photos_processing_retry_at", table_name="photos")
    op.drop_index("ix_photos_processing_lease_until", table_name="photos")
    for name in (
        "processing_retry_at",
        "processing_dispatch_until",
        "processing_attempts",
        "processing_lease_until",
        "processing_started_at",
        "processing_token",
    ):
        op.drop_column("photos", name)
    op.drop_column("users", "agent_run_token")
