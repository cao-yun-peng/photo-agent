"""Skill 表：官方 + 用户自建的生图配方."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    # owner_id NULL 表示官方 Skill
    owner_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    # 参考图 OSS key 列表
    reference_keys: Mapped[list[str]] = mapped_column(JSONB, default=list)
    cover_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model: Mapped[str] = mapped_column(String(32), default="wanx2.1-imageedit", nullable=False)
    # wanx2.1-imageedit API 功能模式：description_edit / stylization_all / stylization_local 等
    function: Mapped[str] = mapped_column(
        String(32), default="description_edit", nullable=False
    )
    # 修改幅度 0.0~1.0，值越大风格变化越强烈
    strength: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
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
        return f"<Skill id={self.id} name={self.name!r} official={self.is_official}>"
