"""Build candidates only for a specific, still-current search generation."""

import asyncio
import json
import logging
from datetime import date
from uuid import UUID

from arq import create_pool

from app.config import settings
from app.services.search_time import current_timezone, _zone
from app.core.telemetry import enqueue_job_with_trace, inject_trace_context
from app.database import AsyncSessionLocal
from app.services.search_engine import SearchService
from app.services.search_contracts import SearchRequest
from app.services.search_budget import RequestTimeout, search_execution
from app.services.search_candidate_pool import (
    _scope_parts,
    claim_prefetch,
    get_prefetch_status,
    push_verified_candidates,
    set_candidate_trace_context,
    set_prefetch_status,
)

logger = logging.getLogger(__name__)
_pool = None

_SEARCH_OPTIONS = {
    "from_date",
    "to_date",
    "tags",
    "scene",
    "objects",
    "text_in_image",
    "mood",
    "colors",
    "photo_types",
    "is_selfie",
    "people_count_min",
    "people_count_max",
    "min_semantic_score",
    "status",
    "auto_parse",
    "verify_constraints",
    "verify_semantic",
    "w_semantic",
    "w_recency",
    "w_interaction",
}


def _prefetch_result_status(result, pushed=0):
    if (
        result.get("next_cursor")
        or result.get("search_pending")
        or result.get("stop_reason") == "request_timeout"
        or result.get("error_type") == "request_timeout"
    ):
        return "resumable"
    if result.get("ok") is False or result.get("stop_reason") in {
        "deadline_exceeded",
        "budget_exhausted",
        "verification_unavailable",
        "verification_incomplete",
        "candidate_limit",
        "snapshot_changed",
        "semantic_scope_unverified",
        "plan_expired",
    }:
        return "failed"
    if result.get("search_exhausted") or result.get("result_set_complete"):
        return "ready" if pushed else "exhausted"
    # Missing completeness evidence cannot prove that all matches were scanned.
    # No automatic requeue: user continuation resumes the persisted plan.
    return "resumable"


async def prefetch_search_candidates(
    ctx, session_id, user_id, query, exclude_photo_ids, search_options=None
):
    scope = session_id  # v2 wire argument; contains user:session:generation.
    try:
        scoped_user, _, _ = _scope_parts(scope)
    except ValueError:
        return {"ok": False, "reason": "legacy_job_discarded"}
    if scoped_user != str(user_id) or not await set_prefetch_status(scope, "running"):
        return {"ok": False, "reason": "stale_search"}
    if not (search_options or {}).get("plan_id"):
        await set_prefetch_status(scope, "failed")
        return {"ok": False, "reason": "missing_query_plan"}
    options = {k: v for k, v in (search_options or {}).items() if k in _SEARCH_OPTIONS}
    outer_timeout = min(
        30.0,
        settings.agent_search_visual_budget_seconds,
        settings.search_total_timeout_seconds,
    )
    execution_timeout = max(
        0.001, outer_timeout - settings.task_cleanup_timeout_seconds
    )

    async def execute():
        _zone.set((search_options or {}).get("timezone_name") or current_timezone())
        for key in ("from_date", "to_date"):
            if isinstance(options.get(key), str):
                options[key] = date.fromisoformat(options[key])
        options.setdefault("w_semantic", 0.9)
        options.setdefault("w_recency", 0.05)
        options.setdefault("w_interaction", 0.05)
        # This is a new execution of the existing plan: fresh local clock,
        # unchanged shared plan/call quota, even in an in-process worker runner.
        with search_execution(execution_timeout, fresh=True):
            async with AsyncSessionLocal() as db:
                result = await SearchService(db, UUID(user_id)).search(
                    SearchRequest(
                        q=query,
                        limit=1,
                        exclude_photo_ids=exclude_photo_ids,
                        candidate_pool_size=settings.agent_search_candidate_pool_size,
                        cursor=(search_options or {}).get("cursor"),
                        **options,
                    ),
                    plan_id=search_options["plan_id"],
                    prefetch=True,
                )
                return {"ok": True, **result}

    from app.services.task_lifecycle import cancel_and_wait, run_cleanup

    task = asyncio.create_task(execute())

    async def watch_generation():
        try:
            while True:
                await asyncio.sleep(0.5)
                if await get_prefetch_status(scope) == "missing":
                    task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            task.cancel()

    watcher = asyncio.create_task(watch_generation())
    execution_timer = asyncio.timeout(outer_timeout)
    try:
        async with execution_timer:
            result = await task
        if not result.get("ok"):
            await set_prefetch_status(scope, _prefetch_result_status(result))
            return {"ok": False, "reason": result.get("error_type", "search_failed")}
        items = [*result.get("items", []), *result.get("_candidate_pool_items", [])]
        excluded = set(map(str, exclude_photo_ids))
        unique = {
            str(item["id"]): item
            for item in items
            if item.get("id") and str(item["id"]) not in excluded
        }
        pushed = await push_verified_candidates(scope, list(unique.values()))
        await set_candidate_trace_context(scope, inject_trace_context())
        status = _prefetch_result_status(result, pushed)
        accepted = await set_prefetch_status(scope, status)
        return {
            "ok": accepted,
            "verified_count": pushed,
            "prefetch_status": status,
            "next_cursor": result.get("next_cursor"),
        }
    except asyncio.CancelledError:
        # Engine cancellation checkpoints the plan; generation-guarded status
        # retains resumability and cannot publish into a newer search.
        await run_cleanup(lambda: set_prefetch_status(scope, "resumable"))
        raise
    except (TimeoutError, RequestTimeout) as exc:
        if isinstance(exc, TimeoutError) and not execution_timer.expired():
            await run_cleanup(lambda: set_prefetch_status(scope, "failed"))
            return {"ok": False, "reason": "prefetch_failed"}
        await run_cleanup(lambda: set_prefetch_status(scope, "resumable"))
        return {
            "ok": False,
            "reason": "request_timeout",
            "prefetch_status": "resumable",
        }
    except Exception:
        logger.exception("search prefetch failed")
        await run_cleanup(lambda: set_prefetch_status(scope, "failed"))
        return {"ok": False, "reason": "prefetch_failed"}
    finally:
        await cancel_and_wait(task, watcher)


async def enqueue_search_prefetch(
    *, session_id, user_id, query, exclude_photo_ids, search_options=None
):
    global _pool
    if not (search_options or {}).get("plan_id"):
        return False
    claimed = await claim_prefetch(session_id)
    if claimed is None:
        return False
    if claimed == 0:
        return True  # Existing job/pool: do not clear or mark it failed.
    try:
        from app.workers.tasks import WorkerSettings

        if _pool is None:
            _pool = await create_pool(WorkerSettings.redis_settings)
        options = json.loads(json.dumps(search_options or {}, default=str))
        options["timezone_name"] = current_timezone()
        await enqueue_job_with_trace(
            _pool,
            "prefetch_search_candidates",
            session_id,
            str(user_id),
            query,
            exclude_photo_ids,
            options,
            _job_id=f"search-prefetch:v2:{session_id}",
        )
        return True
    except BaseException:
        await set_prefetch_status(session_id, "failed")
        raise
