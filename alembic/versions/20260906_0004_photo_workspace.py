"""Stable selections, explicit memory and albums."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
from alembic import op

revision = "20260906_0004"
down_revision = "20260906_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "photo_workspaces",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "albums",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_albums_user_id", "albums", ["user_id"])
    op.create_table(
        "album_members",
        sa.Column(
            "album_id",
            UUID(as_uuid=True),
            sa.ForeignKey("albums.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "photo_id",
            UUID(as_uuid=True),
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("album_id", "position", name="uq_album_position"),
    )
    op.create_table(
        "workspace_actions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("before", JSONB(), nullable=False),
        sa.Column("undone", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_workspace_action_key"
        ),
    )
    op.create_index("ix_workspace_actions_user_id", "workspace_actions", ["user_id"])


def downgrade():
    op.execute(
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM photo_workspaces WHERE revision>0) OR EXISTS (SELECT 1 FROM albums) THEN RAISE EXCEPTION 'Workspace data prevents downgrade'; END IF; END $$"""
    )
    for name in ("workspace_actions", "album_members", "albums", "photo_workspaces"):
        op.drop_table(name)
