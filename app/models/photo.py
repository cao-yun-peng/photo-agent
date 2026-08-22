"""Photo 表：照片元数据 + 语义向量."""

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CHAR,
    DateTime,
    ForeignKey,
    Integer,
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
    __table_args__ = (UniqueConstraint("user_id", "hash", name="uq_photos_user_hash"),)

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
    ai_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=dict)
    # v5 集合检索字段。保留为独立列，确保“全部自拍/截图/合照”可以通过
    # 硬条件扫描，而不是依赖近似向量召回。
    photo_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    is_selfie: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    people_count: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # 处理状态：pending / preflight_check / processing /
    # partial_done / skipped / done / failed
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, index=True
    )

    # 部分成功/跳过/失败的原因码（供前端展示和离线统计用）
    partial_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # embedding 专项补算状态；不重复调用已经成功的 VL。
    embedding_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    embedding_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    embedding_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 只保存稳定错误码/异常类型，不保存第三方响应正文或密钥。
    embedding_last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @property
    def search_index_status(self) -> str:
        """面向客户端的搜索索引状态，不暴露 embedding 技术细节。"""
        if self.embedding is not None and self.status in {"done", "partial_done"}:
            return "ready"
        if self.status in {"pending", "processing"}:
            return "indexing"
        if self.partial_reason == "embedding_service_busy":
            return "service_busy"
        if self.partial_reason in {
            "embedding_missing",
            "embedding_degraded",
            "embedding_retrying",
        }:
            return "retrying"
        return "unavailable"

    @property
    def search_index_message(self) -> str:
        messages = {
            "ready": "智能搜索已就绪",
            "indexing": "正在建立智能搜索",
            "retrying": "智能搜索服务繁忙，正在继续尝试",
            "service_busy": "智能搜索服务暂时繁忙，恢复后将继续尝试",
            "unavailable": "暂时无法通过文字搜索找到这张照片",
        }
        return messages[self.search_index_status]

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
