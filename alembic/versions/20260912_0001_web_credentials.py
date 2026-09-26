"""Add password login without changing existing WeChat identities."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260912_0001"
down_revision = "20260907_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "users", "wechat_openid", existing_type=sa.String(128), nullable=True
    )
    op.create_table(
        "web_credentials",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.UniqueConstraint("username", name="uq_web_credentials_username"),
    )


def downgrade():
    # Fail before removing credentials. Roll back the application image while
    # keeping this additive schema when Web users already exist.
    occupied = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM web_credentials) "
                "OR EXISTS (SELECT 1 FROM users WHERE wechat_openid IS NULL)"
            )
        )
        .scalar()
    )
    if occupied:
        raise RuntimeError("Cannot downgrade Web authentication while Web users exist")
    op.drop_table("web_credentials")
    op.alter_column(
        "users", "wechat_openid", existing_type=sa.String(128), nullable=False
    )
