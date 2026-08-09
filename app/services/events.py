"""用户行为事件写入服务 — Phase 2 个性化数据源.

设计原则：
- 所有埋点统一走 log_event()，避免散落在各处的 INSERT 逻辑；
- 使用独立 Session 提交，避免干扰业务事务；
- payload 保持扁平、可 JSON 序列化，便于画像聚合和离线分析。
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.user_event import UserEvent

logger = logging.getLogger(__name__)

# 当前支持的事件类型。新增类型时需在聚合 worker 里补充消费逻辑。
EVENT_TYPES = frozenset(
    {
        "generation_complete",  # AI 改造完成（含成功/失败）
        "search_click",         # 用户点击搜索结果
        "skill_browse",         # 浏览 Skill 详情/广场
        "photo_interact",       # 与单张照片交互（查看/收藏等）
    }
)


async def log_event(
    user_id: UUID,
    event_type: str,
    payload: dict | None = None,
    db: AsyncSession | None = None,
) -> UserEvent:
    """写入一条用户行为事件。

    Args:
        user_id: 用户 ID。
        event_type: 事件类型，必须是 EVENT_TYPES 之一。
        payload: 业务自定义数据，如 {"skill_id": "...", "status": "done"}。
        db: 可选外部 Session。不传则内部开独立 Session 并立即提交，
            避免与业务事务耦合。

    Returns:
        已持久化的 UserEvent 对象（独立 Session 模式下已 detach）。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知 event_type={event_type}，可选：{sorted(EVENT_TYPES)}")

    event = UserEvent(
        user_id=user_id,
        event_type=event_type,
        payload=payload or {},
    )

    if db is None:
        async with AsyncSessionLocal() as session:
            session.add(event)
            await session.commit()
            await session.refresh(event)
            logger.debug(
                "event logged | type=%s user=%s id=%s", event_type, user_id, event.id
            )
            return event

    #  caller 自己控制事务，只 flush 不 commit
    db.add(event)
    await db.flush()
    logger.debug("event flushed | type=%s user=%s id=%s", event_type, user_id, event.id)
    return event
