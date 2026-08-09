"""存量照片结构化分析迁移任务。

背景：Phase 1 之前的照片只有 ai_description 和 embedding，没有 ai_analysis。
本任务通过独立 ARQ Worker 队列，限速、渐进式地把存量照片升级为结构化分析。
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.photo import Photo
from app.services import ai as ai_service
from app.services.events import log_event
from app.services.oss import sign_get_url

logger = logging.getLogger(__name__)


async def migrate_photos_batch(
    ctx: dict[str, Any],
    batch_size: int = 50,
    only_user_id: str | None = None,
) -> dict[str, Any]:
    """ARQ 任务：逐批迁移存量照片到结构化分析。

    选取条件：
      - status in ("done", "partial_done")
      - ai_analysis 为空 JSONB 或 None
      - 可选限定 user_id（用于单用户修复）

    返回 {"processed": int, "upgraded": int, "failed": int}
    """
    async with AsyncSessionLocal() as db:
        conds = [
            Photo.status.in_(["done", "partial_done"]),
            Photo.ai_analysis.is_(None) | (Photo.ai_analysis == {}),
        ]
        if only_user_id:
            conds.append(Photo.user_id == UUID(only_user_id))

        stmt = (
            select(Photo)
            .where(*conds)
            .order_by(Photo.created_at)
            .limit(batch_size)
        )
        result = await db.execute(stmt)
        photos = result.scalars().all()

        upgraded = 0
        failed = 0
        for photo in photos:
            try:
                image_url = sign_get_url(photo.oss_key, ttl=600)
                analysis = await ai_service.analyze_image(image_url)

                photo.ai_analysis = analysis.model_dump(exclude_none=True)
                # 如果结构化分析降级到了自由描述，更新 ai_description 保证搜索质量
                if analysis.parse_quality != "ok" and analysis.summary:
                    photo.ai_description = analysis.summary

                await db.commit()
                upgraded += 1
                logger.info(
                    "migrate_photos_batch upgraded | photo=%s parse_quality=%s",
                    photo.id,
                    analysis.parse_quality,
                )
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                failed += 1
                logger.exception(
                    "migrate_photos_batch failed | photo=%s exc=%s", photo.id, exc
                )

        processed = len(photos)
        logger.info(
            "migrate_photos_batch done | processed=%s upgraded=%s failed=%s",
            processed,
            upgraded,
            failed,
        )
        return {
            "ok": True,
            "processed": processed,
            "upgraded": upgraded,
            "failed": failed,
        }


async def log_migration_event(
    ctx: dict[str, Any],
    processed: int,
    upgraded: int,
    failed: int,
) -> dict[str, Any]:
    """可选：把迁移批次结果写入 user_events，方便后台观察进度。"""
    await log_event(
        user_id=None,
        event_type="migration_batch",
        payload={
            "processed": processed,
            "upgraded": upgraded,
            "failed": failed,
        },
    )
    return {"ok": True}
