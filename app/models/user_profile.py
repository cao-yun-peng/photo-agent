"""用户画像表 — 聚合后的偏好数据."""
from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserProfile(Base):
    """用户画像表。

    聚合 UserEvent 中的行为信号，形成可用于个性化排序和推荐的偏好数据。
    每日凌晨由异步任务批量更新，避免事件写入时实时计算。
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Skill 偏好度 {skill_id: score}
    skill_affinity: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # 标签亲和度 {tag_name: score}
    tag_affinity: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # 风格分布向量（用于风格匹配），1024 维与照片 embedding 对齐
    style_distribution: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True
    )

    # 统计信息
    total_generations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_searches: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default="now()",
        onupdate="now()",
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<UserProfile user={self.user_id}>"
