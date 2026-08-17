"""后台构建 Agent 续搜候选池。"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from arq import create_pool

from app.config import settings
from app.database import AsyncSessionLocal
from app.services.agent_tools import search_photos
from app.services.search_candidate_pool import (
    clear_candidate_pool,
    push_verified_candidates,
    set_prefetch_status,
)

logger = logging.getLogger(__name__)
_pool = None


async def prefetch_search_candidates(
    ctx: dict[str, Any],
    session_id: str,
    user_id: str,
    query: str,
    exclude_photo_ids: list[str],
) -> dict[str, Any]:
    """在 Worker 时限内完成多批判同，前台请求不等待该任务。"""

    del ctx
    await set_prefetch_status(session_id, "running")
    try:
        async with AsyncSessionLocal() as db:
            result = await search_photos(
                user_id=UUID(user_id),
                db=db,
                query=query,
                limit=1,
                exclude_photo_ids=exclude_photo_ids,
                result_mode="browse",
                verified_only=True,
                candidate_pool_size=settings.agent_search_candidate_pool_size,
                force_visual_verify=settings.agent_search_visual_fallback,
                include_index_coverage=False,
                w_semantic=0.9,
                w_recency=0.05,
                w_interaction=0.05,
            )
        if not result.get("ok"):
            await set_prefetch_status(session_id, "failed")
            return {"ok": False, "reason": result.get("error_type", "search_failed")}
        items = [*result.get("items", []), *result.get("_candidate_pool_items", [])]
        excluded = {str(value) for value in exclude_photo_ids}
        unique: dict[str, dict] = {}
        for item in items:
            photo_id = str(item.get("id", "")) if isinstance(item, dict) else ""
            if photo_id and photo_id not in excluded:
                unique.setdefault(photo_id, item)
        pushed = await push_verified_candidates(session_id, list(unique.values()))
        await set_prefetch_status(session_id, "ready" if pushed else "exhausted")
        return {"ok": True, "verified_count": pushed}
    except Exception as exc:  # noqa: BLE001
        logger.exception("search candidate prefetch failed | session=%s", session_id)
        await set_prefetch_status(session_id, "failed")
        return {"ok": False, "reason": type(exc).__name__}


async def enqueue_search_prefetch(
    *,
    session_id: str,
    user_id: str,
    query: str,
    exclude_photo_ids: list[str],
) -> bool:
    """投递幂等后台预取任务。"""

    from app.workers.tasks import WorkerSettings

    global _pool
    await clear_candidate_pool(session_id)
    await set_prefetch_status(session_id, "queued")
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    digest = hashlib.sha1(
        json.dumps(
            [query, sorted(exclude_photo_ids)], ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()[:16]
    job = await _pool.enqueue_job(
        "prefetch_search_candidates",
        session_id,
        user_id,
        query,
        exclude_photo_ids,
        _job_id=f"search-prefetch:{session_id}:{digest}",
    )
    if job is None:
        await set_prefetch_status(session_id, "failed")
        return False
    return True
