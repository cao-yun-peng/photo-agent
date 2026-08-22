"""照片搜索索引状态和 embedding 重试策略的纯函数。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.photo import Photo


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


async def get_index_coverage(db: AsyncSession, user_id: Any) -> dict[str, Any]:
    """返回用户相册的文字搜索索引覆盖情况，供 API 与 Agent 共用。"""

    retry_reasons = {
        "embedding_missing",
        "embedding_degraded",
        "embedding_retrying",
        "embedding_service_busy",
        "embedding_retry_enqueue_failed",
    }
    result = await db.execute(
        select(
            func.count(Photo.id),
            func.count(Photo.id).filter(
                Photo.status.in_(("done", "partial_done")),
                Photo.embedding.is_not(None),
            ),
            func.count(Photo.id).filter(
                or_(
                    Photo.status.in_(("pending", "processing")),
                    Photo.partial_reason.in_(retry_reasons),
                )
            ),
            func.count(Photo.id).filter(
                Photo.photo_type.is_not(None),
                Photo.is_selfie.is_not(None),
                Photo.people_count.is_not(None),
                Photo.ai_analysis["analysis_version"].astext == "v5",
                Photo.ai_analysis["parse_quality"].astext == "ok",
            ),
        ).where(Photo.user_id == user_id)
    )
    if not hasattr(result, "one"):
        return {
            "total_photos": 0,
            "indexed_photos": 0,
            "retrying_photos": 0,
            "unavailable_photos": 0,
            "coverage_ratio": 1.0,
            "complete": True,
            "message": None,
            "faceted_photos": 0,
            "facet_coverage_ratio": 1.0,
            "semantic_complete": True,
            "semantic_message": None,
        }

    values = [int(value or 0) for value in result.one()]
    total, indexed, retrying = values[:3]
    faceted = values[3] if len(values) > 3 else 0
    unavailable = max(0, total - indexed - retrying)
    ratio = round(indexed / total, 4) if total else 1.0
    message = None
    if retrying or unavailable:
        parts = []
        if retrying:
            parts.append(f"{retrying} 张仍在建立智能搜索")
        if unavailable:
            parts.append(f"{unavailable} 张暂时无法被文字检索")
        message = "；".join(parts) + "，当前结果可能不完整"
    facet_ratio = round(faceted / total, 4) if total else 1.0
    semantic_message = None
    if faceted < total:
        semantic_message = (
            f"{total - faceted} 张尚未完成 v5 语义重索引，"
            "自拍、截图、合照等集合结果可能有遗漏"
        )
    return {
        "total_photos": total,
        "indexed_photos": indexed,
        "retrying_photos": retrying,
        "unavailable_photos": unavailable,
        "coverage_ratio": ratio,
        "complete": indexed == total,
        "message": message,
        "faceted_photos": faceted,
        "facet_coverage_ratio": facet_ratio,
        "semantic_complete": faceted == total,
        "semantic_message": semantic_message,
    }


async def enqueue_index_repairs(
    db: AsyncSession,
    user_id: Any,
    *,
    limit: int = 10,
) -> int:
    """幂等地为可补算 embedding 的照片触发后台修复。"""

    retryable_reasons = {
        "embedding_missing",
        "embedding_degraded",
        "embedding_retry_exhausted",
        "embedding_retry_enqueue_failed",
    }
    result = await db.execute(
        select(Photo)
        .where(
            Photo.user_id == user_id,
            Photo.embedding.is_(None),
            Photo.status == "partial_done",
            Photo.partial_reason.in_(retryable_reasons),
            Photo.ai_description.is_not(None),
            Photo.ai_analysis.is_not(None),
        )
        .limit(max(1, min(limit, 50)))
    )
    photos = list(result.scalars().all())
    if not photos:
        return 0

    # 先标记为 retrying，后续相同搜索不会重复入队。
    for photo in photos:
        photo.partial_reason = "embedding_retrying"
        photo.embedding_next_retry_at = None
    await db.commit()

    from app.workers.tasks import enqueue_retry_photo_embedding

    queued = 0
    for photo in photos:
        try:
            was_queued = await enqueue_retry_photo_embedding(photo.id)
        except Exception:  # noqa: BLE001
            was_queued = False
        if was_queued:
            queued += 1
        else:
            photo.partial_reason = "embedding_retry_enqueue_failed"
    if queued != len(photos):
        await db.commit()
    return queued
