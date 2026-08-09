"""ARQ Worker：处理照片上传后的 AI 管道。

一次 process_photo 的完整链路
--------------------------------
1. 从 PG 拿到 Photo 记录，标记 status=processing。
2. 从 OSS 拉原图字节（mock 模式走本地磁盘）。
3. 用 Pillow 抽 EXIF（拍摄时间 / GPS）+ 生成 512px 缩略图。
4. 缩略图上传回 OSS，写 thumb_key。
5. 调 qwen-vl-plus 生成中文描述（真模式需要 image 是公网可访问 URL）。
6. 调 text-embedding-v3 把描述编码成 1024 维向量。
7. 更新 Photo：ai_description、embedding、width/height、taken_at、location、status=done。

任何一步失败：status=failed，日志里留原因。用户再次上传相同 hash 会 409 去重，
需要手动清除或重跑迁移才能重试；MVP 阶段接受这个约束。

启动命令：`arq app.workers.tasks.WorkerSettings`
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from arq.connections import RedisSettings
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.photo import Photo
from app.services import ai as ai_service
from app.services import image as image_service
from app.services.oss import (
    get_object,
    put_object,
    sign_get_url,
    thumb_key_of,
)
from app.services.circuit_breaker import ServiceDegradedError
from app.services.metrics import metrics
from app.services.quality import (
    can_transition,
    decide_storage,
    preflight_check,
    quality_gate,
)
from app.workers.gen_tasks import NonRetryableError
from app.workers.lifecycle_tasks import archive_cold_events, count_events_by_age
from app.workers.migrate_tasks import migrate_photos_batch
from app.workers.profile_tasks import (
    aggregate_all_profiles,
    aggregate_user_profile,
)


logger = logging.getLogger(__name__)


async def process_photo(ctx: dict[str, Any], photo_id: str) -> dict[str, Any]:
    """处理一张照片。返回 dict 便于 ARQ 结果面板查看。"""
    logger.info("process_photo start | photo_id=%s", photo_id)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Photo).where(Photo.id == UUID(photo_id)))
        photo = result.scalar_one_or_none()
        if photo is None:
            logger.warning("process_photo skipped, photo not found | id=%s", photo_id)
            return {"ok": False, "reason": "photo_not_found"}

        # --- 1) 标记 processing --------------------------------
        if not can_transition(photo.status, "processing"):
            logger.warning(
                "process_photo invalid transition | id=%s current=%s",
                photo_id,
                photo.status,
            )
            return {
                "ok": False,
                "photo_id": photo_id,
                "status": photo.status,
                "reason": "invalid_state_transition",
            }
        photo.status = "processing"
        await db.commit()

        try:
            # --- 2) 拉原图 ------------------------------------
            raw = await get_object(photo.oss_key)
            if raw is None:
                raise NonRetryableError(f"OSS object missing: {photo.oss_key}")

            # --- 3) 输入预检 ----------------------------------
            preflight = await asyncio.to_thread(preflight_check, raw)
            if not preflight.ok:
                photo.status = "skipped"
                photo.partial_reason = preflight.reason
                photo.width = preflight.width
                photo.height = preflight.height
                await db.commit()
                logger.warning(
                    "process_photo skipped | id=%s reason=%s",
                    photo_id,
                    preflight.reason,
                )
                return {
                    "ok": False,
                    "photo_id": photo_id,
                    "status": "skipped",
                    "reason": preflight.reason,
                }

            # --- 4) EXIF + 缩略图 -----------------------------
            processed = await asyncio.to_thread(image_service.process, raw, thumb_max=512)

            # --- 5) 上传缩略图 --------------------------------
            thumb_key = thumb_key_of(photo.oss_key)
            await put_object(thumb_key, processed.thumb_bytes, content_type="image/jpeg")

            # --- 6) 调 VL 生描述 + 结构化分析 ------------------
            # 真模式下 DashScope 要用公网 URL 拉图；mock 模式下 URL 不重要
            image_url = sign_get_url(photo.oss_key, ttl=600)
            try:
                description = await ai_service.describe_image(image_url)
            except ServiceDegradedError:
                # VL 降级：保留 EXIF + 缩略图，状态 partial_done
                photo.width = processed.width
                photo.height = processed.height
                photo.taken_at = processed.taken_at
                photo.location = processed.location
                photo.thumb_key = thumb_key
                photo.status = "partial_done"
                photo.partial_reason = "vl_degraded"
                await db.commit()
                metrics.record_photo_status("partial_done")
                logger.warning(
                    "process_photo vl degraded | id=%s", photo_id
                )
                return {
                    "ok": True,
                    "photo_id": photo_id,
                    "status": "partial_done",
                    "degraded": True,
                }

            analysis = await ai_service.analyze_image(image_url)

            # --- 7) 调 Embedding ------------------------------
            # embedding 用结构化 summary + 描述混合，更稳健
            embed_text_input = f"{description}\n{analysis.summary}".strip()
            try:
                embedding = await ai_service.embed_text(embed_text_input)
            except ServiceDegradedError:
                # Embedding 降级：有描述/分析但无向量，仍可结构化过滤
                embedding = None
                partial_reason = "embedding_degraded"
            else:
                partial_reason = None

            # --- 8) 输出质量关卡 ------------------------------
            gate = quality_gate(
                description=description,
                embedding=embedding,
                analysis=analysis,
            )

            # 若 embedding 降级，强制 storage_tier 为 partial
            if partial_reason and gate.storage_tier == "full":
                gate.storage_tier = "partial"
                gate.reason = "partial"
                gate.issues.append(partial_reason)

            # --- 9) 根据质量分级回写状态 -----------------------
            decision = decide_storage(gate)

            photo.width = processed.width
            photo.height = processed.height
            photo.taken_at = processed.taken_at
            photo.location = processed.location
            photo.thumb_key = thumb_key

            if decision.store_description:
                photo.ai_description = description
            else:
                photo.ai_description = None

            if decision.store_analysis:
                photo.ai_analysis = analysis.model_dump(exclude_none=True)
            else:
                photo.ai_analysis = {}

            if decision.store_embedding:
                photo.embedding = embedding
            else:
                photo.embedding = None

            if not can_transition(photo.status, decision.status):
                # 正常情况下不会发生，兜底保持当前状态
                logger.error(
                    "process_photo unexpected transition | id=%s from=%s to=%s",
                    photo_id,
                    photo.status,
                    decision.status,
                )
                photo.status = "failed"
                photo.partial_reason = "state_machine_error"
            else:
                photo.status = decision.status
                photo.partial_reason = decision.partial_reason

            await db.commit()
            metrics.record_photo_status(photo.status)
            logger.info(
                "process_photo done | id=%s status=%s desc=%r size=%dx%d",
                photo_id,
                photo.status,
                description[:40],
                processed.width,
                processed.height,
            )
            return {
                "ok": photo.status in ("done", "partial_done"),
                "photo_id": photo_id,
                "status": photo.status,
                "description": description[:60],
                "size": [processed.width, processed.height],
            }

        except NonRetryableError as exc:
            # 不可重试：OSS 对象缺失、数据完整性问题等
            photo.status = "failed"
            await db.commit()
            metrics.record_photo_status("failed")
            logger.warning("process_photo non-retryable failure | id=%s err=%s", photo_id, exc)
            return {"ok": False, "photo_id": photo_id, "error": str(exc)}

        except Exception:  # noqa: BLE001
            # 可重试：网络超时、服务降级、AI 调用失败等
            # 标记 failed 后 re-raise，让 ARQ max_tries 机制触发重试
            photo.status = "failed"
            await db.commit()
            metrics.record_photo_status("failed")
            logger.exception("process_photo retryable failure | id=%s", photo_id)
            raise


# --- 生产者辅助函数 ------------------------------------------------------

_pool = None  # 全局连接池，避免每次入队都握手


async def enqueue_process_photo(photo_id) -> None:
    """API 层调用：把 photo 入队等 worker 消费。"""
    from arq import create_pool

    global _pool
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    await _pool.enqueue_job("process_photo", str(photo_id))


async def enqueue_generate_photo(generation_id) -> None:
    """API 层调用：把 generation 入队让 worker 跑 AI 改造。"""
    from arq import create_pool

    global _pool
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    await _pool.enqueue_job("generate_photo", str(generation_id))


async def enqueue_profile_update(user_id) -> None:
    """API 层调用：把画像聚合任务入队。"""
    from arq import create_pool

    global _pool
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    await _pool.enqueue_job("aggregate_user_profile", str(user_id))


# --- ARQ Worker 配置 -----------------------------------------------------


class WorkerSettings:
    """ARQ 启动配置，命令行会读取这个类."""

    # 延迟 import 避免循环
    from app.workers.gen_tasks import generate_photo

    functions = [
        process_photo,
        generate_photo,
        aggregate_user_profile,
        aggregate_all_profiles,
        migrate_photos_batch,
        archive_cold_events,
        count_events_by_age,
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 180         # 生图较慢，给 3 分钟
    keep_result = 600
    max_tries = 2
