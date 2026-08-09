"""Agent 会话状态表 — 支撑多轮对话和兜底追踪."""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentSession(Base):
    """Agent 会话状态表。

    记录一次 Agent 任务的完整状态：搜索次数、使用过的策略、
    用户否定过的结果 ID 等。配合 Redis 分布式锁使用，不直接存储锁状态。
    """

    __tablename__ = "agent_sessions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # {search_attempts, last_query, rejected_ids, strategy, ...}

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )
    # active | completed | abandoned

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return f"<AgentSession id={self.id} user={self.user_id} status={self.status}>"
