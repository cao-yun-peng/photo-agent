"""Agent 续搜候选池：Redis 原子消费、状态与短等待。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import settings
from app.services.lock import get_redis

logger = logging.getLogger(__name__)


def candidate_pool_key(session_id: Any) -> str:
    return f"agent:search-pool:{session_id}"


def candidate_status_key(session_id: Any) -> str:
    return f"agent:search-prefetch-status:{session_id}"


def candidate_trace_key(session_id: Any) -> str:
    return f"agent:search-prefetch-trace:{session_id}"


async def clear_candidate_pool(session_id: Any) -> None:
    try:
        redis = await get_redis()
        await redis.delete(
            candidate_pool_key(session_id),
            candidate_status_key(session_id),
            candidate_trace_key(session_id),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate pool clear degraded: %s", exc)


async def set_prefetch_status(session_id: Any, status: str) -> None:
    try:
        redis = await get_redis()
        await redis.setex(
            candidate_status_key(session_id),
            settings.agent_search_pool_ttl_seconds,
            status,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate status write degraded: %s", exc)


async def get_prefetch_status(session_id: Any) -> str:
    try:
        value = await (await get_redis()).get(candidate_status_key(session_id))
        return str(value or "missing")
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate status read degraded: %s", exc)
        return "missing"


async def set_candidate_trace_context(
    session_id: Any, carrier: dict[str, str]
) -> None:
    """保存预取 Worker 的 span 上下文，供后续会话用 Span Link 关联。"""
    if not carrier:
        return
    try:
        redis = await get_redis()
        await redis.setex(
            candidate_trace_key(session_id),
            settings.agent_search_pool_ttl_seconds,
            json.dumps(carrier),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate trace write degraded: %s", exc)


async def get_candidate_trace_context(session_id: Any) -> dict[str, str] | None:
    try:
        raw = await (await get_redis()).get(candidate_trace_key(session_id))
        value = json.loads(raw) if raw else None
        if isinstance(value, dict):
            return {str(key): str(item) for key, item in value.items()}
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("candidate trace payload invalid: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate trace read degraded: %s", exc)
    return None


async def push_verified_candidates(session_id: Any, items: list[dict]) -> int:
    """把已验证候选写入 Redis；列表顺序即后续展示顺序。"""

    if not items:
        return 0
    try:
        redis = await get_redis()
        key = candidate_pool_key(session_id)
        payloads = [json.dumps(item, ensure_ascii=False) for item in items]
        await redis.rpush(key, *payloads)
        await redis.expire(key, settings.agent_search_pool_ttl_seconds)
        return len(payloads)
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate pool write degraded: %s", exc)
        return 0


async def pop_verified_candidate(session_id: Any) -> dict | None:
    """原子取出下一张候选；损坏数据会跳过而不是阻断续搜。"""

    try:
        redis = await get_redis()
        key = candidate_pool_key(session_id)
        while raw := await redis.lpop(key):
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("id"):
                return item
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate pool pop degraded: %s", exc)
        return None


async def candidate_pool_size(session_id: Any) -> int:
    try:
        return int(await (await get_redis()).llen(candidate_pool_key(session_id)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("candidate pool size degraded: %s", exc)
        return 0


async def wait_for_verified_candidate(
    session_id: Any,
    *,
    timeout_seconds: float | None = None,
) -> dict | None:
    """短暂等待后台预取，绝不占满 Agent 的整轮工具预算。"""

    timeout_seconds = (
        settings.agent_search_prefetch_wait_seconds
        if timeout_seconds is None
        else max(0.0, timeout_seconds)
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        item = await pop_verified_candidate(session_id)
        if item is not None:
            return item
        status = await get_prefetch_status(session_id)
        if status not in {"queued", "running"} or time.monotonic() >= deadline:
            return None
        await asyncio.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
