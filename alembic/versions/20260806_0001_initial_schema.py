"""Initial schema: users, photos, tags, photo_tags + pgvector extension.

Revision ID: 20260806_0001
Revises:
Create Date: 2026-08-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260806_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 依赖扩展
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_uuid()

    # users
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("wechat_openid", sa.String(128), nullable=False, unique=True),
        sa.Column("nickname", sa.String(64)),
        sa.Column("avatar_url", sa.String(512)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_users_wechat_openid", "users", ["wechat_openid"])

    # photos
    op.create_table(
        "photos",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("oss_key", sa.String(512), nullable=False),
        sa.Column("thumb_key", sa.String(512)),
        sa.Column("hash", sa.CHAR(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("mime_type", sa.String(64)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("taken_at", sa.DateTime(timezone=True)),
        sa.Column("location", postgresql.JSONB()),
        sa.Column("ai_description", sa.Text()),
        sa.Column("embedding", Vector(1024)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "hash", name="uq_photos_user_hash"),
    )
    op.create_index("ix_photos_user_id", "photos", ["user_id"])
    op.create_index("ix_photos_hash", "photos", ["hash"])
    op.create_index("ix_photos_taken_at", "photos", ["taken_at"])
    op.create_index("ix_photos_status", "photos", ["status"])
    op.create_index(
        "idx_photos_user_taken",
        "photos",
        ["user_id", sa.text("taken_at DESC")],
    )
    # HNSW 向量索引（pgvector ≥ 0.5）
    op.execute(
        "CREATE INDEX idx_photos_emb ON photos "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # tags
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "name", name="uq_tags_user_name"),
    )
    op.create_index("ix_tags_user_id", "tags", ["user_id"])

    # photo_tags
    op.create_table(
        "photo_tags",
        sa.Column(
            "photo_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default="ai"),
    )


def downgrade() -> None:
    op.drop_table("photo_tags")
    op.drop_index("ix_tags_user_id", table_name="tags")
    op.drop_table("tags")
    op.execute("DROP INDEX IF EXISTS idx_photos_emb")
    op.drop_index("idx_photos_user_taken", table_name="photos")
    op.drop_index("ix_photos_status", table_name="photos")
    op.drop_index("ix_photos_taken_at", table_name="photos")
    op.drop_index("ix_photos_hash", table_name="photos")
    op.drop_index("ix_photos_user_id", table_name="photos")
    op.drop_table("photos")
    op.drop_index("ix_users_wechat_openid", table_name="users")
    op.drop_table("users")
    # 保留 extension 不删，避免影响其他数据库使用者
