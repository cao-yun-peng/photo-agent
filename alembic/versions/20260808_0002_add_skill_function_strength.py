"""Add function and strength columns to skills table.

Revision ID: 20260808_0002
Revises: 20260808_0001
Create Date: 2026-08-08

新增：
- skills.function：wanx2.1-imageedit API 功能模式（description_edit / stylization_all 等）
- skills.strength：修改幅度 0.0~1.0，控制风格变化强度

背景：之前 image_gen.py 硬编码 function=description_edit + strength=0.7，
导致风格转换类 Skill（如 3D 泡泡玛特、宫崎骏动画风）效果不佳。
现在每个 Skill 可以独立配置 function 和 strength。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0002"
down_revision: Union[str, None] = "20260808_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "skills",
        sa.Column(
            "function",
            sa.String(32),
            nullable=False,
            server_default="description_edit",
        ),
    )
    op.add_column(
        "skills",
        sa.Column(
            "strength",
            sa.Float(),
            nullable=False,
            server_default="0.7",
        ),
    )


def downgrade() -> None:
    op.drop_column("skills", "strength")
    op.drop_column("skills", "function")
