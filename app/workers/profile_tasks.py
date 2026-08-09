"""画像聚合 Worker 任务.

运行方式：
- 单用户：arq app.workers.tasks.WorkerSettings 后，通过 enqueue_profile_update(user_id) 入队；
- 全量：命令行 `python -m app.workers.profile_tasks` 会遍历所有有事件的用户并更新画像。
"""
from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import distinct, select

from app.database import AsyncSessionLocal
from app.models.user_event import UserEvent
from app.services.profile import build_user_profile

logger = logging.getLogger(__name__)


async def aggregate_user_profile(ctx: dict, user_id: str) -> dict:
    """ARQ 任务：为单个用户重建画像。"""
    uid = UUID(user_id)
    async with AsyncSessionLocal() as db:
        profile = await build_user_profile(uid, db)
        await db.commit()
        return {
            "ok": True,
            "user_id": user_id,
            "skill_affinity": profile.skill_affinity,
            "tag_affinity": profile.tag_affinity,
            "has_style_vector": profile.style_distribution is not None,
            "total_generations": profile.total_generations,
            "total_searches": profile.total_searches,
        }


async def aggregate_all_profiles(ctx: dict | None = None) -> dict:
    """ARQ 任务 / 管理命令：重建所有有事件用户的画像。"""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(distinct(UserEvent.user_id))
            )
        ).scalars().all()

    updated = 0
    failed = 0
    for uid in rows:
        try:
            result = await aggregate_user_profile({}, str(uid))
            if result.get("ok"):
                updated += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("aggregate profile failed | user=%s", uid, exc_info=exc)

    return {"ok": True, "updated": updated, "failed": failed, "total": len(rows)}


async def main() -> None:
    """命令行入口：python -m app.workers.profile_tasks"""
    logging.basicConfig(level=logging.INFO)
    result = await aggregate_all_profiles()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
