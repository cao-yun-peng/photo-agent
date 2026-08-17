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

VL 之前的可重试错误由 ARQ 重跑整条任务；embedding 失败则保存已经完成的 VL 产物，
进入最多 5 次真实调用的专项补算，不重复理解图片。

启动命令：`arq app.workers.tasks.WorkerSettings`
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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
from app.services.circuit_breaker import (
    ServiceDegradedError,
    embedding_breaker,
)
from app.services.metrics import metrics
from app.services.quality import (
    can_transition,
    decide_storage,
    preflight_check,
    quality_gate,
    summarize_quality_reason,
)
from app.services.search_index import retry_delay_after_failure
from app.workers.gen_tasks import NonRetryableError
from app.workers.lifecycle_tasks import archive_cold_events, count_events_by_age
from app.workers.migrate_tasks import migrate_photos_batch
from app.workers.profile_tasks import (
    aggregate_all_profiles,
    aggregate_user_profile,
)


logger = logging.getLogger(__name__)


def _safe_embedding_error(exc: Exception) -> str:
    """只保存异常类型，不把第三方响应正文或潜在凭据写入数据库。"""
    return type(exc).__name__[:64]


async def _schedule_embedding_retry(
    redis,
    photo_id: str,
    *,
    delay_seconds: int,
    retry_count: int,
    scheduled_at: datetime,
) -> bool:
    job_id = (
        f"embedding-retry:{photo_id}:{retry_count}:"
        f"{int(scheduled_at.timestamp())}"
    )
    try:
        job = await redis.enqueue_job(
            "retry_photo_embedding",
            photo_id,
            _defer_by=delay_seconds,
            _job_id=job_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("embedding retry enqueue failed | photo_id=%s", photo_id)
        return False
    return job is not None


async def retry_photo_embedding(ctx: dict[str, Any], photo_id: str) -> dict[str, Any]:
    """只补算 embedding；保留已成功的 VL 描述和结构化分析。"""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Photo).where(Photo.id == UUID(photo_id)))
        photo = result.scalar_one_or_none()
        if photo is None:
            return {"ok": False, "reason": "photo_not_found"}
        if photo.embedding is not None and photo.status == "done":
            return {"ok": True, "status": "done", "reason": "already_ready"}
        if photo.status != "partial_done" or photo.partial_reason not in {
            "embedding_missing",
            "embedding_degraded",
            "embedding_retrying",
            "embedding_service_busy",
            "embedding_retry_enqueue_failed",
        }:
            return {
                "ok": False,
                "status": photo.status,
                "reason": "not_retryable",
            }
        if not photo.ai_description or not photo.ai_analysis:
            photo.partial_reason = "embedding_source_missing"
            photo.embedding_next_retry_at = None
            await db.commit()
            return {"ok": False, "status": "partial_done", "reason": "source_missing"}

        from app.schemas.analysis import ImageAnalysis

        try:
            analysis = ImageAnalysis.model_validate(photo.ai_analysis)
        except ValueError:
            photo.partial_reason = "embedding_source_invalid"
            photo.embedding_next_retry_at = None
            await db.commit()
            return {"ok": False, "status": "partial_done", "reason": "source_invalid"}

        retrieval_text = ai_service.build_retrieval_text(
            photo.ai_description, analysis
        )
        photo.embedding_next_retry_at = None
        photo.embedding_last_attempt_at = now
        await db.commit()

        try:
            embedding = await ai_service.embed_text(retrieval_text)
        except ServiceDegradedError as exc:
            # 熔断直接拒绝，没有真实模型调用，不消耗单图尝试次数。
            delay = max(1, embedding_breaker.retry_after_seconds())
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            photo.partial_reason = "embedding_service_busy"
            photo.embedding_last_error = "circuit_open"
            photo.embedding_next_retry_at = scheduled_at
            await db.commit()
            queued = await _schedule_embedding_retry(
                ctx["redis"],
                photo_id,
                delay_seconds=delay,
                retry_count=photo.embedding_retry_count,
                scheduled_at=scheduled_at,
            )
            if not queued:
                photo.partial_reason = "embedding_retry_enqueue_failed"
                photo.embedding_next_retry_at = None
                await db.commit()
            return {
                "ok": False,
                "status": "partial_done",
                "reason": _safe_embedding_error(exc),
                "attempted": False,
                "retry_count": photo.embedding_retry_count,
                "retry_in_seconds": delay if queued else None,
            }
        except Exception as exc:  # noqa: BLE001
            failure_count = int(photo.embedding_retry_count or 0) + 1
            photo.embedding_retry_count = failure_count
            photo.embedding_last_error = _safe_embedding_error(exc)
            delay = retry_delay_after_failure(failure_count)
            if delay is None:
                photo.partial_reason = "embedding_retry_exhausted"
                photo.embedding_next_retry_at = None
                await db.commit()
                return {
                    "ok": False,
                    "status": "partial_done",
                    "reason": "embedding_retry_exhausted",
                    "attempted": True,
                    "retry_count": failure_count,
                }
            scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            photo.partial_reason = "embedding_retrying"
            photo.embedding_next_retry_at = scheduled_at
            await db.commit()
            queued = await _schedule_embedding_retry(
                ctx["redis"],
                photo_id,
                delay_seconds=delay,
                retry_count=failure_count,
                scheduled_at=scheduled_at,
            )
            if not queued:
                photo.partial_reason = "embedding_retry_enqueue_failed"
                photo.embedding_next_retry_at = None
                await db.commit()
            return {
                "ok": False,
                "status": "partial_done",
                "reason": _safe_embedding_error(exc),
                "attempted": True,
                "retry_count": failure_count,
                "retry_in_seconds": delay if queued else None,
            }

        gate = quality_gate(
            description=photo.ai_description,
            embedding=embedding,
            analysis=analysis,
        )
        if gate.storage_tier != "full":
            # 服务返回了畸形向量：按统一质量门禁阻断，不进入重试风暴。
            decision = decide_storage(gate)
            photo.status = decision.status
            photo.partial_reason = decision.partial_reason
            photo.ai_description = (
                None if not decision.store_description else photo.ai_description
            )
            photo.ai_analysis = (
                {} if not decision.store_analysis else photo.ai_analysis
            )
            photo.embedding = None
            photo.embedding_next_retry_at = None
            photo.embedding_last_error = summarize_quality_reason(gate.issues)
            await db.commit()
            return {"ok": False, "status": photo.status, "reason": photo.partial_reason}

        photo.embedding = embedding
        photo.status = "done"
        photo.partial_reason = None
        photo.embedding_retry_count = 0
        photo.embedding_next_retry_at = None
        photo.embedding_last_error = None
        await db.commit()
        metrics.record_photo_status("done")
        return {"ok": True, "status": "done", "retry_count": 0}


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
            # 展开细粒度视觉字段，确保动作/年龄/模糊/位置真正进入召回向量。
            embed_text_input = ai_service.build_retrieval_text(description, analysis)
            embedding_retry_delay: int | None = None
            embedding_retry_attempted = False
            photo.embedding_last_attempt_at = datetime.now(timezone.utc)
            try:
                embedding = await ai_service.embed_text(embed_text_input)
            except ServiceDegradedError:
                # OPEN/HALF_OPEN 已拒绝本次请求：没有真正调用模型，不计次数。
                embedding = None
                partial_reason = "embedding_service_busy"
                photo.embedding_last_error = "circuit_open"
                embedding_retry_delay = max(
                    1, embedding_breaker.retry_after_seconds()
                )
            except Exception as exc:  # noqa: BLE001
                # 第一次真实调用失败，从失败结束时开始等待 2 秒再补算。
                embedding = None
                embedding_retry_attempted = True
                photo.embedding_retry_count = 1
                photo.embedding_last_error = _safe_embedding_error(exc)
                partial_reason = "embedding_retrying"
                embedding_retry_delay = retry_delay_after_failure(1)
            else:
                partial_reason = None
                photo.embedding_retry_count = 0
                photo.embedding_next_retry_at = None
                photo.embedding_last_error = None

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
                # embedding 服务失败时，用客户端可理解的精确状态覆盖通用
                # quality_gate 的 embedding_missing。
                photo.partial_reason = partial_reason or decision.partial_reason

            if embedding_retry_delay is not None:
                photo.embedding_next_retry_at = datetime.now(
                    timezone.utc
                ) + timedelta(seconds=embedding_retry_delay)

            await db.commit()

            # 先持久化可用的 VL 结果，再安排只补 embedding 的延迟任务。
            if embedding_retry_delay is not None:
                queued = await _schedule_embedding_retry(
                    ctx["redis"],
                    photo_id,
                    delay_seconds=embedding_retry_delay,
                    retry_count=photo.embedding_retry_count,
                    scheduled_at=photo.embedding_next_retry_at,
                )
                if not queued:
                    photo.partial_reason = "embedding_retry_enqueue_failed"
                    photo.embedding_next_retry_at = None
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
                "embedding_attempted": embedding_retry_attempted,
                "embedding_retry_in_seconds": (
                    embedding_retry_delay
                    if embedding_retry_delay is not None and queued
                    else None
                ),
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


async def enqueue_retry_photo_embedding(photo_id) -> bool:
    """API 层调用：立即补算已耗尽/入队失败照片的搜索向量。"""
    from arq import create_pool

    global _pool
    if _pool is None:
        _pool = await create_pool(WorkerSettings.redis_settings)
    scheduled_at = datetime.now(timezone.utc)
    return await _schedule_embedding_retry(
        _pool,
        str(photo_id),
        delay_seconds=0,
        retry_count=0,
        scheduled_at=scheduled_at,
    )


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
        retry_photo_embedding,
        generate_photo,
        aggregate_user_profile,
        aggregate_all_profiles,
        migrate_photos_batch,
        archive_cold_events,
        count_events_by_age,
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = 180         # 生图较慢，给 3 分钟
    keep_result = 600
    max_tries = 2
