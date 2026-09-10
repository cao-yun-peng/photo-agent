"""Owned package cancellation, bounded re-planning and stale-attempt recovery."""

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select, or_
from app.config import settings
from app.services.ai import _is_mock
from app.services.provider_ledger import plan_once
from app.models.generation import Generation, GenerationInput
from app.models.user import User
from app.schemas.creative_plan import PackageOptions
from app.services.generation_service import (
    GenerationDomainError,
    release_reserved_quota,
)
from app.services.package_execution import (
    validate_execution,
    plan_images,
    title_policy,
    digest,
    PackageExecutionError,
)

ACTIVE = {
    "awaiting_confirmation",
    "pending",
    "queue_failed",
    "processing",
    "cancel_requested",
}


async def owned(db, user_id, generation_id, lock=False):
    query = (
        select(Generation)
        .where(Generation.id == generation_id, Generation.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    gen = (await db.execute(query)).scalar_one_or_none()
    if gen is None:
        raise GenerationDomainError("generation_not_found", "生成任务不存在", 404)
    if not gen.execution_snapshot:
        raise GenerationDomainError("package_required", "此操作仅支持流程包任务", 409)
    return gen


async def cancel_generation(db, user_id, generation_id):
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    gen = await owned(db, user_id, generation_id, lock=True)
    if gen.status in {"cancelled", "cancel_requested"}:
        return gen
    if gen.status not in ACTIVE:
        raise GenerationDomainError(
            "generation_state_invalid", "任务已结束，不能取消", 409
        )
    if gen.status == "processing" and gen.progress_stage != "validating":
        gen.status = "cancel_requested"
        gen.error_message = "已请求停止；供应商可能仍在处理，费用不保证撤销。"
    else:
        gen.status = gen.progress_stage = "cancelled"
        gen.lease_token = gen.lease_expires_at = None
        await release_reserved_quota(db, gen)
    gen.confirmation_token = None
    await db.commit()
    return gen


async def prepare_iteration(db, user_id, generation_id, feedback, idempotency_key):
    feedback = feedback.strip()
    if not feedback:
        raise GenerationDomainError("feedback_required", "请说明需要调整的内容", 422)
    parent = await owned(db, user_id, generation_id)
    if parent.status != "done":
        raise GenerationDomainError(
            "iteration_not_ready",
            "仅对已完成的结果创建调整方案；结果未知时请先核实供应商状态",
            409,
        )
    root_id = parent.root_generation_id or parent.id
    signature = digest([str(parent.id), parent.execution_digest, feedback])

    async def check():
        rows = (
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == user_id,
                        or_(
                            Generation.id == root_id,
                            Generation.root_generation_id == root_id,
                        ),
                    )
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        existing = next((g for g in rows if g.idempotency_key == idempotency_key), None)
        if existing:
            if existing.execution_snapshot.get("iteration_request_digest") != signature:
                raise GenerationDomainError(
                    "idempotency_conflict", "修改要求已变化，请使用新任务标识", 409
                )
            return existing, rows
        if any(g.status in ACTIVE for g in rows if g.id != parent.id):
            raise GenerationDomainError(
                "iteration_in_progress", "已有调整方案或任务，请先处理或取消", 409
            )
        if len(rows) - 1 >= settings.generation_max_iterations:
            raise GenerationDomainError(
                "iteration_limit", "本组任务已达到调整次数上限", 409
            )
        cost = (
            sum(Decimal(str(g.estimated_cost_yuan)) for g in rows)
            + parent.estimated_cost_yuan
        )
        if cost > Decimal(str(settings.generation_chain_estimate_limit_yuan)):
            raise GenerationDomainError(
                "iteration_budget", "本组任务的累计费用估算达到上限", 409
            )
        return None, rows

    existing, _ = await check()
    if existing:
        return existing
    try:
        inputs = await validate_execution(db, parent)
    except PackageExecutionError as exc:
        raise GenerationDomainError("snapshot_changed", str(exc), 409) from exc
    snapshot = copy.deepcopy(parent.execution_snapshot)
    options = PackageOptions.model_validate(snapshot["title_options"])
    try:
        plan, planning_id = await plan_once(
            db,
            user_id,
            idempotency_key,
            signature,
            lambda: plan_images(
                snapshot["prompt"], inputs[0], inputs[1:], options, feedback
            ),
        )
    except GenerationDomainError:
        raise
    except Exception as exc:
        raise GenerationDomainError(
            "package_plan_failed", "调整方案生成失败，请稍后重试", 422
        ) from exc
    try:
        lettering = title_policy(plan, options, feedback)
    except PackageExecutionError as exc:
        raise GenerationDomainError("invalid_title", str(exc), 422) from exc
    snapshot.update(
        plan=plan.model_dump(),
        planning_operation_id=planning_id,
        planner_mode="mock" if _is_mock() else "vision",
        prompt=plan.production_prompt
        + "\n"
        + lettering
        + "\n第1张为主体，其余仅供风格参考。画幅："
        + snapshot["size"],
        iteration_request_digest=signature,
        revision_feedback=feedback,
    )
    # Shared planning claim precedes calls; recheck group limits under owner lock.
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    existing, rows = await check()
    if existing:
        return existing
    # A key cannot be reused from an unrelated task owned by the same user.
    collision = (
        await db.execute(
            select(Generation.id).where(
                Generation.user_id == user_id,
                Generation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if collision:
        raise GenerationDomainError(
            "idempotency_conflict", "任务标识已被其他任务使用", 409
        )
    try:
        await validate_execution(db, parent)
    except PackageExecutionError as exc:
        raise GenerationDomainError("snapshot_changed", str(exc), 409) from exc
    snapshot.update(
        parent_generation_id=str(parent.id),
        root_generation_id=str(root_id),
        iteration_index=len(rows),
    )
    gen = Generation(
        user_id=user_id,
        source_photo_id=parent.source_photo_id,
        skill_id=parent.skill_id,
        parent_generation_id=parent.id,
        root_generation_id=root_id,
        iteration_index=len(rows),
        extra_prompt=feedback,
        model=parent.model,
        status="awaiting_confirmation",
        progress_stage="awaiting_confirmation",
        execution_snapshot=snapshot,
        execution_digest=digest(snapshot),
        estimated_cost_yuan=parent.estimated_cost_yuan,
        idempotency_key=idempotency_key,
        confirmation_token=uuid4(),
        confirmation_expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=settings.generation_confirmation_ttl_seconds),
    )
    db.add(gen)
    await db.flush()
    for position, content in enumerate(inputs):
        db.add(
            GenerationInput(
                generation_id=gen.id,
                position=position,
                content=content,
                media_type="image/png",
            )
        )
    await db.commit()
    return gen


async def recover_generations(db, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=settings.generation_lease_seconds)
    rows = (
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.execution_snapshot.is_not(None),
                    or_(
                        (Generation.status.in_(["processing", "cancel_requested"]))
                        & (
                            or_(
                                Generation.lease_expires_at <= now,
                                Generation.lease_expires_at.is_(None)
                                & (Generation.updated_at <= cutoff),
                            )
                        ),
                        (Generation.status == "pending")
                        & (Generation.updated_at <= cutoff),
                        (Generation.status == "awaiting_confirmation")
                        & (Generation.confirmation_expires_at <= now),
                    ),
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    recovered = 0
    for gen in rows:
        if gen.progress_stage in {"async_waiting", "async_polling"} and (
            gen.verification or {}
        ).get("provider_task"):
            # A crashed GET is safe to resume; never restart the paid submission.
            gen.progress_stage = "async_waiting"
            gen.lease_token = gen.lease_expires_at = None
            continue
        recovered += 1
        if gen.status == "pending":
            gen.status = "queue_failed"
            gen.enqueue_status = "failed"
            gen.error_message = "队列等待超时，请重试入队或取消任务。"
        elif gen.status == "awaiting_confirmation":
            gen.status = "expired"
        elif gen.progress_stage == "validating":
            gen.status = "failed"
            gen.error_message = "执行准备中断，未进入模型调用。"
        else:
            gen.status = "outcome_unknown"
            gen.error_message = "执行中断，供应商结果与费用未知；不会自动重新生成。"
        gen.progress_stage = gen.status
        gen.lease_token = gen.lease_expires_at = None
        if gen.status != "queue_failed":
            gen.confirmation_token = None
            await release_reserved_quota(db, gen)
    await db.commit()
    return recovered
