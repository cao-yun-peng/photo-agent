"""存量照片 v5 结构化分析和 embedding 重索引任务。

背景：Phase 1 之前的照片只有 ai_description 和 embedding，没有 ai_analysis。
本任务通过独立 ARQ Worker 队列，限速、渐进式地把存量照片升级为结构化分析。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select

from app.database import AsyncSessionLocal
from app.models.photo import Photo
from app.services import ai as ai_service
from app.services.events import log_event
from app.services.oss import sign_get_url
from app.services.semantic_facets import apply_semantic_facets

logger = logging.getLogger(__name__)


def _semantic_reindex_needed():
    return or_(
        Photo.ai_analysis.is_(None),
        Photo.photo_type.is_(None),
        Photo.is_selfie.is_(None),
        Photo.people_count.is_(None),
        Photo.ai_analysis["analysis_version"].astext.is_(None),
        Photo.ai_analysis["analysis_version"].astext
        != ai_service.VL_ANALYSIS_PROMPT_VERSION,
    )


async def count_pending_semantic_reindex(only_user_id: str | None = None) -> int:
    """只统计待重索引数量，不触发 VL/embedding 调用。"""

    async with AsyncSessionLocal() as db:
        conds = [
            Photo.status.in_(["done", "partial_done"]),
            _semantic_reindex_needed(),
        ]
        if only_user_id:
            conds.append(Photo.user_id == UUID(only_user_id))
        value = await db.scalar(select(func.count(Photo.id)).where(*conds))
        return int(value or 0)


async def migrate_photos_batch(
    ctx: dict[str, Any],
    batch_size: int = 50,
    only_user_id: str | None = None,
) -> dict[str, Any]:
    """ARQ 任务：逐批升级到 v5 语义字段并重新生成 embedding。

    选取条件：
      - status in ("done", "partial_done")
      - v5 分析缺失、降级，或任一集合字段尚未建立
      - 可选限定 user_id（用于单用户修复）

    返回 {"processed": int, "upgraded": int, "failed": int}
    """
    async with AsyncSessionLocal() as db:
        conds = [
            Photo.status.in_(["done", "partial_done"]),
            _semantic_reindex_needed(),
        ]
        if only_user_id:
            conds.append(Photo.user_id == UUID(only_user_id))

        stmt = (
            select(Photo)
            .where(*conds)
            .order_by(Photo.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
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
                apply_semantic_facets(photo, analysis)
                # embedding 必须同步重建，否则新字段虽然入库却不参与普通语义召回。
                description = photo.ai_description or analysis.summary
                photo.embedding = await ai_service.embed_text(
                    ai_service.build_retrieval_text(description, analysis)
                )
                if not photo.ai_description and analysis.summary:
                    photo.ai_description = analysis.summary
                if photo.status == "partial_done" and photo.embedding is not None:
                    photo.status = "done"
                    photo.partial_reason = None

                await db.commit()
                upgraded += 1
                logger.info(
                    "migrate_photos_batch upgraded | photo=%s parse_quality=%s version=%s",
                    photo.id,
                    analysis.parse_quality,
                    analysis.analysis_version,
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
