"""ARQ 任务：generate_photo —— 对已有照片做 AI 改造."""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.generation import Generation
from app.models.rate_limit import RateLimit
from app.models.skill import Skill
from app.models.photo import Photo
from app.services import image_gen, oss
from app.services.circuit_breaker import image_gen_breaker
from app.services.events import log_event
from app.services.metrics import metrics
from app.services.quality import can_transition

logger = logging.getLogger(__name__)


class NonRetryableError(Exception):
    """不可重试的异常：数据缺失、非法输入等业务错误。

    ARQ 的 max_tries 机制仅对 re-raise 的异常生效；
    抛出此异常的 except 分支会捕获并标记 failed，
    而其他异常会被 re-raise 交给 ARQ 重试。
    """


async def generate_photo(ctx: dict[str, Any], generation_id: str) -> dict[str, Any]:
    """
    完整链路：
      1. 从 PG 读 Generation 记录 → processing
      2. 找到 skill + source_photo
      3. 拼 prompt（skill.prompt_template + extra_prompt）
      4. 把 source + refs 签成公网 URL
      5. 调 image_gen.generate()
      6. 下载生成结果 → 存 OSS
      7. 更新记录 → done
      8. 增加 rate_limit + skill.use_count
    """
    logger.info("generate_photo start | id=%s", generation_id)

    async with AsyncSessionLocal() as db:
        row = await db.execute(
            select(Generation).where(Generation.id == UUID(generation_id))
        )
        gen = row.scalar_one_or_none()
        if gen is None:
            return {"ok": False, "reason": "generation_not_found"}

        # --- 1) 状态机校验 ----------------------------------
        if not can_transition(gen.status, "processing"):
            logger.warning(
                "generate_photo invalid transition | id=%s current=%s",
                generation_id,
                gen.status,
            )
            return {
                "ok": False,
                "generation_id": generation_id,
                "status": gen.status,
                "reason": "invalid_state_transition",
            }
        gen.status = "processing"
        await db.commit()

        try:
            # 拿 Skill 与源照片
            skill = None
            if gen.skill_id:
                sk_row = await db.execute(
                    select(Skill).where(Skill.id == gen.skill_id)
                )
                skill = sk_row.scalar_one_or_none()

            source = None
            if gen.source_photo_id:
                p_row = await db.execute(
                    select(Photo).where(Photo.id == gen.source_photo_id)
                )
                source = p_row.scalar_one_or_none()
            if source is None:
                raise NonRetryableError("source photo not found")

            # 拼 prompt
            base_prompt = (skill.prompt_template if skill else "把这张照片进行 AI 改造")
            if gen.extra_prompt:
                prompt = f"{base_prompt}\n附加要求：{gen.extra_prompt}"
            else:
                prompt = base_prompt

            # 签公网 URL
            source_url = oss.sign_get_url(source.oss_key, ttl=1800)
            ref_urls = [
                oss.sign_get_url(k, ttl=1800)
                for k in (skill.reference_keys if skill else [])
            ]

            # 调模型（经熔断器保护，服务降级时快速失败）
            # 从 Skill 读取 function 和 strength，实现按 Skill 精细控制风格变化幅度
            gen_function = skill.function if skill else "description_edit"
            gen_strength = skill.strength if skill else 0.7
            async with metrics.timeit("image_gen", tags={"model": gen.model}):
                result = await image_gen_breaker.call(
                    image_gen.generate,
                    source_image_url=source_url,
                    prompt=prompt,
                    reference_urls=ref_urls,
                    model=gen.model,
                    function=gen_function,
                    strength=gen_strength,
                )

            # 万相返回临时 URL，OpenAI 返回 Base64；统一转成 bytes 后存入 OSS。
            if result.image_bytes is not None:
                image_bytes = result.image_bytes
                content_type = result.content_type
            elif result.image_url:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.get(result.image_url)
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"download result HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                image_bytes = resp.content
                content_type = resp.headers.get("content-type", "image/jpeg")
                content_type = content_type.partition(";")[0].strip().lower()
            else:
                raise RuntimeError("image generation returned no image data")

            new_key = _build_gen_key(gen.user_id, content_type)
            await oss.put_object(new_key, image_bytes, content_type=content_type)

            gen.result_oss_key = new_key
            gen.status = "done"
            gen.cost_yuan = Decimal(str(result.cost_yuan))
            await db.commit()

            # 更新配额与 Skill 使用计数（原子操作，避免并发竞态）
            await _bump_rate_limit(db, gen.user_id)
            if skill is not None:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(Skill)
                    .where(Skill.id == skill.id)
                    .values(use_count=Skill.use_count + 1)
                )
                await db.commit()

            metrics.record_photo_status("generation_done")
            logger.info(
                "generate_photo done | id=%s key=%s cost=%.4f",
                generation_id, new_key, float(result.cost_yuan),
            )
            photo_tags = (
                source.ai_analysis.get("objects", [])[:10]
                if source and source.ai_analysis
                else []
            )
            await log_event(
                user_id=gen.user_id,
                event_type="generation_complete",
                payload={
                    "generation_id": str(gen.id),
                    "source_photo_id": str(gen.source_photo_id) if gen.source_photo_id else None,
                    "skill_id": str(gen.skill_id) if gen.skill_id else None,
                    "model": gen.model,
                    "status": gen.status,
                    "cost_yuan": float(gen.cost_yuan),
                    "photo_tags": photo_tags,
                    "scene": source.ai_analysis.get("scene") if source and source.ai_analysis else None,
                    "mood": source.ai_analysis.get("mood") if source and source.ai_analysis else None,
                },
            )
            return {"ok": True, "generation_id": generation_id, "result_oss_key": new_key}

        except NonRetryableError as exc:
            # 不可重试：数据缺失、非法输入等，直接标记 failed 并返回
            gen.status = "failed"
            gen.error_message = str(exc)[:500]
            await db.commit()
            metrics.record_photo_status("generation_failed")
            logger.warning("generate_photo non-retryable failure | id=%s err=%s", generation_id, exc)
            await log_event(
                user_id=gen.user_id,
                event_type="generation_complete",
                payload={
                    "generation_id": str(gen.id),
                    "source_photo_id": str(gen.source_photo_id) if gen.source_photo_id else None,
                    "skill_id": str(gen.skill_id) if gen.skill_id else None,
                    "model": gen.model,
                    "status": gen.status,
                    "error": gen.error_message,
                },
            )
            return {"ok": False, "generation_id": generation_id, "error": str(exc)}

        except Exception as exc:  # noqa: BLE001
            # 可重试：网络超时、服务降级、OSS 抖动等
            # 标记 failed 后 re-raise，让 ARQ max_tries 机制触发重试
            gen.status = "failed"
            gen.error_message = str(exc)[:500]
            await db.commit()
            metrics.record_photo_status("generation_failed")
            logger.exception("generate_photo retryable failure | id=%s", generation_id)
            raise


def _build_gen_key(user_id: UUID, content_type: str = "image/jpeg") -> str:
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    suffix = {
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(content_type, ".jpg")
    return f"generations/{user_id}/{today}/{uuid4().hex}{suffix}"


async def _bump_rate_limit(db, user_id: UUID) -> None:
    """原子 upsert：有则 gen_count + 1，无则插入 1。

    使用 PostgreSQL ON CONFLICT DO UPDATE 保证并发安全，
    避免 SELECT-then-INSERT/UPDATE 的竞态条件。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = date.today()
    stmt = pg_insert(RateLimit).values(
        user_id=user_id, day=today, gen_count=1
    ).on_conflict_do_update(
        index_elements=["user_id", "day"],
        set_={"gen_count": RateLimit.gen_count + 1},
    )
    await db.execute(stmt)
    await db.commit()
