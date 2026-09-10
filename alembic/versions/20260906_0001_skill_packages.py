"""Private immutable Skill packages.

Revision ID: 20260906_0001
Revises: 20260905_0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260906_0001"
down_revision = "20260905_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "skills",
        sa.Column("kind", sa.String(16), nullable=False, server_default="template"),
    )
    op.add_column("skills", sa.Column("current_version_id", sa.UUID(), nullable=True))
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "skill_id",
            sa.UUID(),
            sa.ForeignKey("skills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "skill_id", "content_sha256", name="uq_skill_version_content"
        ),
    )
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
    op.create_table(
        "skill_assets",
        sa.Column(
            "version_id",
            sa.UUID(),
            sa.ForeignKey("skill_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.String(240), primary_key=True),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
    )


def downgrade():
    # Never silently lose user resources or turn packages into template skills.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM skills WHERE kind = 'package') THEN
            RAISE EXCEPTION 'Export and explicitly remove Skill packages before downgrade';
        END IF;
    END $$;""")
    op.drop_table("skill_assets")
    op.drop_table("skill_versions")
    op.drop_column("skills", "current_version_id")
    op.drop_column("skills", "kind")
