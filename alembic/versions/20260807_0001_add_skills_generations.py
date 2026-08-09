"""Add skills, generations, rate_limits tables (D15–D17).

Revision ID: 20260807_0001
Revises: 20260806_0001
Create Date: 2026-08-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260807_0001"
down_revision: Union[str, None] = "20260806_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- skills ----
    op.create_table(
        "skills",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # owner_id NULL 表示官方 Skill
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        # 参考图 OSS key 列表
        sa.Column("reference_keys", postgresql.JSONB(), server_default="[]"),
        sa.Column("cover_key", sa.String(512)),          # 广场展示封面
        sa.Column("model", sa.String(32), nullable=False, server_default="wanx-v1"),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_official", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
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
    op.create_index("ix_skills_owner_id", "skills", ["owner_id"])
    op.create_index("ix_skills_is_public", "skills", ["is_public"])
    op.create_index("ix_skills_is_official", "skills", ["is_official"])

    # ---- generations ----
    op.create_table(
        "generations",
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
        sa.Column(
            "source_photo_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("photos.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "skill_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("skills.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("extra_prompt", sa.Text()),
        sa.Column("result_oss_key", sa.String(512)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text()),
        sa.Column("model", sa.String(32), nullable=False, server_default="wanx-v1"),
        sa.Column("cost_yuan", sa.Numeric(8, 4), server_default="0"),
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
    op.create_index("ix_generations_user_id", "generations", ["user_id"])
    op.create_index("ix_generations_status", "generations", ["status"])
    op.create_index("ix_generations_skill_id", "generations", ["skill_id"])
    op.create_index(
        "idx_generations_user_created",
        "generations",
        ["user_id", sa.text("created_at DESC")],
    )

    # ---- rate_limits ----
    op.create_table(
        "rate_limits",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("gen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("rate_limits")
    op.drop_index("idx_generations_user_created", table_name="generations")
    op.drop_index("ix_generations_skill_id", table_name="generations")
    op.drop_index("ix_generations_status", table_name="generations")
    op.drop_index("ix_generations_user_id", table_name="generations")
    op.drop_table("generations")
    op.drop_index("ix_skills_is_official", table_name="skills")
    op.drop_index("ix_skills_is_public", table_name="skills")
    op.drop_index("ix_skills_owner_id", table_name="skills")
    op.drop_table("skills")
