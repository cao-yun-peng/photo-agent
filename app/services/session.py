"""Agent 会话持久化：把 AgentState 存到 agent_sessions 表，支撑多轮对话."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_session import AgentSession
from app.services.agent_state import AgentState

logger = logging.getLogger(__name__)

_DEFAULT_EXPIRE_MINUTES = 30
_RESUMABLE_SESSION_STATUSES = ("active", "completed")


async def load_session(
    db: AsyncSession,
    session_id: UUID,
    user_id: UUID,
) -> AgentSession | None:
    """加载属于指定用户且仍可续接的会话。

    ``completed`` 表示上一轮已经产生正常最终回复，不代表用户显式关闭了会话。
    在过期时间内允许它继续承载“就这张”“换成狗的”等后续指令；失败、放弃或
    过期会话仍不可恢复。
    """
    now = datetime.now(timezone.utc)
    return (
        await db.execute(
            select(AgentSession).where(
                and_(
                    AgentSession.id == session_id,
                    AgentSession.user_id == user_id,
                    AgentSession.status.in_(_RESUMABLE_SESSION_STATUSES),
                    AgentSession.expires_at > now,
                )
            )
        )
    ).scalar_one_or_none()


async def save_session(
    db: AsyncSession,
    state: AgentState,
    status: str = "active",
    expire_minutes: int = _DEFAULT_EXPIRE_MINUTES,
) -> AgentSession:
    """保存或更新 Agent 会话状态。"""
    session = (
        await db.execute(
            select(AgentSession).where(AgentSession.id == state.session_id)
        )
    ).scalar_one_or_none()

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)

    if session is None:
        session = AgentSession(
            id=state.session_id,
            user_id=state.user_id,
            state=state.to_json(),
            status=status,
            expires_at=expires_at,
        )
        db.add(session)
    else:
        session.state = state.to_json()
        session.status = status
        session.expires_at = expires_at

    await db.flush()
    logger.debug("session saved | id=%s status=%s", state.session_id, status)
    return session
