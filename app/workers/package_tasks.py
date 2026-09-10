"""Single-attempt package worker with durable progress and fenced settlement."""

import logging
import asyncio
import time
from time import perf_counter
from app.services.generation_accounting import generation_cost
from app.services.provider_ledger import call_scope
from app.services.generation_errors import failure_details
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4
from sqlalchemy import select, or_
from app.config import settings
from app.models.generation import Generation
from app.models.skill import Skill
from app.services import image_gen, oss
from app.services.generation_service import (
    consume_reserved_quota,
    release_reserved_quota,
)
from app.services.package_execution import validate_execution, review_output
from app.services import async_image


logger = logging.getLogger(__name__)


async def run_package(factory, generation_id):
    started = perf_counter()
    measurements = {"clock": "monotonic", "scope": "worker_only", "stages_ms": {}}
    gid = UUID(generation_id)
    async with factory() as db:
        gen = (
            await db.execute(
                select(Generation).where(Generation.id == gid).with_for_update()
            )
        ).scalar_one()
        if gen.status == "done":
            return {
                "ok": True,
                "reason": "already_done",
                "generation_id": generation_id,
            }
        provider_task = (gen.verification or {}).get("provider_task")
        resuming = (
            bool(provider_task)
            and gen.status in {"processing", "cancel_requested"}
            and gen.progress_stage in {"async_waiting", "async_polling"}
            and (
                gen.lease_expires_at is None
                or gen.lease_expires_at <= datetime.now(timezone.utc)
            )
        )
        if gen.attempt_count and not resuming:
            return {"ok": False, "reason": "package_attempt_already_consumed"}
        if gen.status != "pending" and not resuming:
            return {"ok": False, "reason": "invalid_state_transition"}
        token = uuid4()
        gen.lease_token = token
        gen.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.generation_lease_seconds
        )
        gen.status, gen.progress_stage, gen.enqueue_status = (
            "cancel_requested" if gen.status == "cancel_requested" else "processing",
            "async_polling" if resuming else "validating",
            "consumed",
        )
        gen.attempt_count = 1
        snapshot, user_id, skill_id, model = (
            gen.execution_snapshot,
            gen.user_id,
            gen.skill_id,
            gen.model,
        )
        await db.commit()

    async def live(db):
        row = (
            await db.execute(
                select(Generation).where(Generation.id == gid).with_for_update()
            )
        ).scalar_one()
        if (
            row.lease_token != token
            or row.status not in {"processing", "cancel_requested"}
            or row.lease_expires_at <= datetime.now(timezone.utc)
        ):
            return None
        return row

    async def settle(
        status,
        *,
        key=None,
        verification=None,
        error=None,
        error_code=None,
        consumed=False,
    ):
        async with factory() as db:
            row = await live(db)
            if row is None:
                return {"ok": False, "reason": "ownership_lost"}
            cancelled = row.status == "cancel_requested"
            row.status = (
                "cancelled" if cancelled and status != "outcome_unknown" else status
            )
            row.progress_stage = row.status
            row.error_message = error
            row.last_error_code = error_code
            measurements["total_ms"] = round((perf_counter() - started) * 1000, 3)
            row.verification = {
                **(row.verification or {}),
                **(verification or {}),
                "execution_metrics": measurements,
            }
            row.result_oss_key = key if row.status == "done" else None
            row.lease_token = row.lease_expires_at = None
            if consumed:
                await consume_reserved_quota(db, row)
            else:
                await release_reserved_quota(db, row)
            if row.status == "done" and skill_id:
                from sqlalchemy import update

                await db.execute(
                    update(Skill)
                    .where(Skill.id == skill_id)
                    .values(use_count=Skill.use_count + 1)
                )
            await db.commit()
            # Telemetry must never turn a settled task back into a failure.
            try:
                from app.workers.gen_tasks import log_event
                from app.services.metrics import metrics

                metrics.record_photo_status(
                    "generation_done" if row.status == "done" else "generation_failed"
                )
                await log_event(
                    user_id=user_id,
                    event_type="generation_complete",
                    payload={
                        "generation_id": generation_id,
                        "status": row.status,
                        "model": model,
                        "cost_yuan": float(row.cost_yuan),
                        "iteration_index": row.iteration_index,
                    },
                )
            except Exception:
                logger.warning(
                    "Generation telemetry failed after settlement", exc_info=True
                )
            return {
                "ok": row.status == "done",
                "generation_id": generation_id,
                "status": row.status,
                "result_oss_key": row.result_oss_key,
            }

    async def wait_for_provider(task, error=None):
        async with factory() as db:
            row = await live(db)
            if row is None:
                return {"ok": False, "reason": "ownership_lost"}
            row.verification = {**(row.verification or {}), "provider_task": task}
            row.progress_stage = "async_waiting"
            row.lease_token = row.lease_expires_at = None
            row.last_error_code = error
            row.error_message = (
                "结果查询暂时中断，稍后继续查询，不会重复生图。" if error else None
            )
            await db.commit()
        return {"ok": True, "status": "processing", "generation_id": generation_id}

    invoked, received = resuming, False
    stage, stage_started = "validating", perf_counter()
    try:
        async with factory() as db:
            row = await live(db)
            if row is None:
                return {"ok": False, "reason": "ownership_lost"}
            frozen = (
                await validate_execution(db, row, check_source=False)
                if resuming
                else await validate_execution(db, row)
            )
            # Persist the invocation boundary before any provider side effect.
            row.progress_stage = "async_polling" if resuming else "generating"
            await db.commit()
        invoked = True
        from app.services.circuit_breaker import image_gen_breaker

        provider_started = perf_counter()
        stage, stage_started = "generating", provider_started
        with call_scope(factory, user_id, "generation", generation_id=gid):
            if resuming:
                if time.time() >= provider_task["deadline"]:
                    raise TimeoutError("Async image deadline exceeded; never resubmit")
                async with asyncio.timeout(90):
                    result = await async_image.poll(
                        provider_task, factory, user_id, gid
                    )
                if result is None:
                    return await wait_for_provider(provider_task)
            elif (
                settings.openai_image_transport == "timicc_async"
                and not image_gen._is_openai_mock()
            ):
                provider_task = await async_image.submit(
                    snapshot["prompt"], frozen, snapshot["size"]
                )
                return await wait_for_provider(provider_task)
            else:
                result = await image_gen_breaker.call(
                    image_gen.generate,
                    source_image_url="",
                    reference_urls=[],
                    prompt=snapshot["prompt"],
                    model=model,
                    function="description_edit",
                    strength=0.7,
                    image_inputs=frozen,
                    size=snapshot["size"],
                )
        received = True
        measurements["stages_ms"]["generation"] = round(
            (perf_counter() - provider_started) * 1000, 3
        )
        measurements["cost"] = generation_cost(result)
        async with factory() as db:
            row = await live(db)
            if row is None:
                return {"ok": False, "reason": "ownership_lost"}
            row.cost_yuan = Decimal(str(result.cost_yuan))
            row.verification = {
                **(row.verification or {}),
                "execution_metrics": measurements,
            }
            row.progress_stage = "verifying"
            cancelled = row.status == "cancel_requested"
            await db.commit()
        if cancelled:
            return await settle(
                "cancelled",
                consumed=True,
                error="已停止展示结果；模型已返回，费用可能已产生。",
            )
        if not result.image_bytes:
            raise ValueError("package provider returned no image bytes")
        review_started = perf_counter()
        stage, stage_started = "verifying", review_started
        with call_scope(factory, user_id, "verification", generation_id=gid):
            verification = await review_output(
                snapshot, frozen, result.image_bytes, simulated=result.model == "mock"
            )
        measurements["stages_ms"]["verification"] = round(
            (perf_counter() - review_started) * 1000, 3
        )
        async with factory() as db:
            row = await live(db)
            if row is None:
                return {"ok": False, "reason": "ownership_lost"}
            cancelled = row.status == "cancel_requested"
            row.progress_stage = "storing"
            await db.commit()
        if cancelled:
            return await settle("cancelled", consumed=True)
        # A late writer uses a unique attempt path; it cannot overwrite a winner.
        key = f"generations/{user_id}/{gid}/{token}.png"
        stage, stage_started = "storing", perf_counter()
        await oss.put_object(key, result.image_bytes, content_type=result.content_type)
        return await settle("done", key=key, verification=verification, consumed=True)
    except Exception as exc:
        import httpx

        # Only GET/download failures are retried; a submitted POST is never replayed.
        transient_poll = isinstance(exc, (httpx.RequestError, TimeoutError)) or (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code in {408, 429, 500, 502, 503, 504}
        )
        if (
            resuming
            and not received
            and transient_poll
            and time.time() < provider_task["deadline"]
        ):
            failure, _ = failure_details(exc)
            return await wait_for_provider(provider_task, failure["code"])
        failure, message = failure_details(exc)
        if isinstance(exc, async_image.AsyncImageFailed):
            failure["code"] = exc.code
            message = (
                "供应商生图通道触发限流，请稍后再试；费用需核实，本次未自动重试。"
                if exc.code == "provider_rate_limited"
                else "图片服务报告任务失败；费用需核实，不会自动重新生成。"
            )
        failure["stage"] = stage
        # A transport error during storage or review is not a generation-provider error.
        if stage != "generating":
            failure["code"] = f"{stage}_failed"
            message = "任务执行未完成。"
        measurements["failure"] = failure
        measurements["failed_stage_ms"] = round(
            (perf_counter() - stage_started) * 1000, 3
        )
        logger.warning(
            "Package generation failed | generation_id=%s stage=%s code=%s exception_type=%s",
            generation_id,
            stage,
            failure["code"],
            failure["exception_type"],
        )
        return await settle(
            "outcome_unknown"
            if invoked
            and not received
            and not isinstance(exc, async_image.AsyncImageFailed)
            else "failed",
            consumed=received,
            error_code=failure["code"],
            error=message + "供应商结果和费用未知，不会自动重试。"
            if invoked
            and not received
            and not isinstance(exc, async_image.AsyncImageFailed)
            else message + "请查看状态后再处理。",
        )


async def recover_generation_jobs(ctx):
    from app.database import AsyncSessionLocal
    from app.services.generation_lifecycle import recover_generations

    async with AsyncSessionLocal() as db:
        recovered = await recover_generations(db)
        ids = (
            (
                await db.execute(
                    select(Generation.id)
                    .where(
                        Generation.status.in_(["processing", "cancel_requested"]),
                        Generation.progress_stage == "async_waiting",
                        or_(
                            Generation.lease_expires_at.is_(None),
                            Generation.lease_expires_at <= datetime.now(timezone.utc),
                        ),
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for gid in ids:
        await ctx["redis"].enqueue_job(
            "generate_photo",
            str(gid),
            _job_id=f"generation-poll:{gid}:{int(time.time()) // 30}",
        )
    return {"recovered": recovered, "async_polls_enqueued": len(ids)}
