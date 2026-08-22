"""Add v5 semantic collection search facets.

Revision ID: 20260822_0001
Revises: 20260815_0001
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0001"
down_revision: Union[str, None] = "20260815_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "photos", sa.Column("photo_type", sa.String(length=32), nullable=True)
    )
    op.add_column("photos", sa.Column("is_selfie", sa.Boolean(), nullable=True))
    op.add_column("photos", sa.Column("people_count", sa.Integer(), nullable=True))

    # 先用旧 v4 JSON 做无模型成本的兼容回填。analysis_version 仍保持 v4，
    # 因此覆盖率不会把这些近似推导误报成完成了 v5 重索引。
    op.execute(
        """
        UPDATE photos
        SET people_count = CASE
                WHEN ai_analysis #>> '{persons,count}' ~ '^\\d+$'
                    THEN (ai_analysis #>> '{persons,count}')::integer
                ELSE 0
            END,
            is_selfie = CASE
                WHEN COALESCE(ai_analysis, '{}'::jsonb) = '{}'::jsonb THEN NULL
                WHEN COALESCE(ai_analysis->'capture_context', '[]'::jsonb) ? '自拍'
                    OR ai_analysis->>'is_selfie' = 'true' THEN true
                ELSE false
            END,
            photo_type = CASE
                WHEN COALESCE(ai_analysis, '{}'::jsonb) = '{}'::jsonb THEN NULL
                WHEN ai_analysis->>'photo_type' IN
                    ('selfie','screenshot','group_photo','portrait','document','food','scenery','other')
                    THEN ai_analysis->>'photo_type'
                WHEN COALESCE(ai_analysis->'capture_context', '[]'::jsonb) ? '自拍'
                    THEN 'selfie'
                WHEN COALESCE(ai_analysis->'capture_context', '[]'::jsonb) ?| ARRAY['截图','屏幕截图']
                    THEN 'screenshot'
                WHEN ai_analysis #>> '{persons,count}' ~ '^\\d+$'
                    AND (ai_analysis #>> '{persons,count}')::integer >= 2 THEN 'group_photo'
                WHEN ai_analysis #>> '{persons,count}' ~ '^\\d+$'
                    AND (ai_analysis #>> '{persons,count}')::integer = 1 THEN 'portrait'
                ELSE 'other'
            END
        WHERE ai_analysis IS NOT NULL
        """
    )
    op.create_index("ix_photos_photo_type", "photos", ["photo_type"])
    op.create_index("ix_photos_is_selfie", "photos", ["is_selfie"])
    op.create_index("ix_photos_people_count", "photos", ["people_count"])


def downgrade() -> None:
    op.drop_index("ix_photos_people_count", table_name="photos")
    op.drop_index("ix_photos_is_selfie", table_name="photos")
    op.drop_index("ix_photos_photo_type", table_name="photos")
    op.drop_column("photos", "people_count")
    op.drop_column("photos", "is_selfie")
    op.drop_column("photos", "photo_type")
