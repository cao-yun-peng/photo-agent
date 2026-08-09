"""Phase 4: production hardening & data migration support.

Revision ID: 20260808_0001
Revises: 20260807_0002
Create Date: 2026-08-08

新增：
- migration_progress 表：追踪存量照片结构化分析迁移进度
- user_event_partitions 元数据表：记录已创建的 PG 分区（若新部署按分区建表）
- 为 user_events.created_at 增加 BRIN 索引（现有非分区表的轻量替代方案）

注意：user_events 已在 20260807_0002 作为普通表创建。
若需原生按月分区，建议在新集群通过重新建表 + 数据迁移完成；
本迁移对现有表不做破坏性转换，仅补充索引和元数据表。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260808_0001"
down_revision: Union[str, None] = "20260807_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- migration_progress：存量迁移进度追踪 ----
    op.create_table(
        "migration_progress",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "task_name",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        # 已处理数量
        sa.Column(
            "processed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # 成功升级数量
        sa.Column(
            "upgraded",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # 失败数量
        sa.Column(
            "failed",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        # 最近一次偏移/标记，如最后处理的 created_at
        sa.Column("last_marker", sa.String(64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ---- user_event_partitions：分区元数据（新部署分区建表时使用）----
    op.create_table(
        "user_event_partitions",
        sa.Column(
            "partition_name",
            sa.String(64),
            primary_key=True,
        ),
        sa.Column(
            "start_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "end_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # ---- 为 user_events.created_at 增加 BRIN 索引 ----
    # 对按时间顺序写入的事件表，BRIN 索引体积小、性能好
    op.create_index(
        "ix_user_events_created_at_brin",
        "user_events",
        ["created_at"],
        postgresql_using="brin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_user_events_created_at_brin",
        table_name="user_events",
    )
    op.drop_table("user_event_partitions")
    op.drop_table("migration_progress")
