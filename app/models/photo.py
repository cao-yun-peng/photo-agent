"""Photo 表：照片元数据 + 语义向量."""
from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# 1024 维对应 DashScope text-embedding-v3；如果模型换了要一并改
EMBEDDING_DIM = 1024


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (
        UniqueConstraint("user_id", "hash", name="uq_photos_user_hash"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 存储
    oss_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumb_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, index=True)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)

    # EXIF 与位置
    taken_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    location: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # AI 产物
    ai_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )

    # 结构化分析结果：{scene, scene_detail, persons, objects,
    # text_in_image, mood, colors, summary}
    ai_analysis: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default=dict
    )

    # 处理状态：pending / preflight_check / processing /
    # partial_done / skipped / done / failed
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )

    # 部分成功/跳过/失败的原因码（供前端展示和离线统计用）
    partial_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Photo id={self.id} status={self.status}>"
