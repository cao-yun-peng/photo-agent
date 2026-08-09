"""用户行为事件表 — 个性化系统的数据源."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserEvent(Base):
    """用户行为事件表。

    记录用户在系统中的关键行为，供画像构建和离线分析使用。
    高频写入，后续需按 created_at 做分区归档。
    """

    __tablename__ = "user_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # generation_complete | search_click | skill_browse | photo_interact

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 事件特定数据，如 {skill_id, photo_tags, query, ...}

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserEvent id={self.id} user={self.user_id} type={self.event_type}>"
