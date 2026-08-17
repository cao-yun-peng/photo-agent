"""照片搜索索引状态和 embedding 重试策略的纯函数。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import settings


def retry_delay_after_failure(failure_count: int) -> int | None:
    """返回本次实际失败结束后的等待秒数；达到总尝试次数后返回 None。"""
    if failure_count <= 0 or failure_count >= settings.embedding_max_attempts:
        return None
    index = failure_count - 1
    delays = settings.embedding_retry_delays_seconds
    if len(delays) != settings.embedding_max_attempts - 1:
        raise ValueError(
            "EMBEDDING_RETRY_DELAYS_SECONDS must contain max_attempts - 1 values"
        )
    return max(0, int(delays[index]))


def processing_status(photo: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """生成不暴露第三方异常正文的客户端轮询状态。"""
    now = now or datetime.now(timezone.utc)
    next_retry_at = getattr(photo, "embedding_next_retry_at", None)
    next_retry_in_seconds = None
    if next_retry_at is not None:
        if next_retry_at.tzinfo is None:
            next_retry_at = next_retry_at.replace(tzinfo=timezone.utc)
        next_retry_in_seconds = max(0, int((next_retry_at - now).total_seconds()))
    return {
        "photo_id": photo.id,
        "photo_status": photo.status,
        "search_index_status": photo.search_index_status,
        "retry_count": int(getattr(photo, "embedding_retry_count", 0) or 0),
        "max_attempts": settings.embedding_max_attempts,
        "next_retry_at": next_retry_at,
        "next_retry_in_seconds": next_retry_in_seconds,
        "message": photo.search_index_message,
    }
