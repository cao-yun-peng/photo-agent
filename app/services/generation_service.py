"""生成准备、确认、幂等和额度预占的领域服务。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.generation import Generation
from app.models.photo import Photo
from app.models.rate_limit import RateLimit
from app.models.skill import Skill
from app.services.metrics import metrics
from app.services.rollout import agent_variant_for_user
from app.schemas.creative_plan import PackageOptions
from app.services.package_execution import (
    PackageExecutionError,
    build_snapshot,
    digest,
    request_digest,
    validate_execution,
)
from app.models.generation import GenerationInput
from app.models.user import User


class GenerationDomainError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


async def prepare_generation(
    *,
    db: AsyncSession,
    user_id: UUID,
    photo_id: UUID,
    skill_id: UUID | None = None,
    extra_prompt: str | None = None,
    model: str | None = None,
    idempotency_key: str | None = None,
    package_options: PackageOptions | None = None,
) -> Generation:
    """创建待确认任务；同一用户的幂等键重复提交返回原任务。"""

    package_options = PackageOptions.model_validate(package_options or {})

    if idempotency_key:
        existing = (
            await db.execute(
                select(Generation).where(
                    Generation.user_id == user_id,
                    Generation.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.execution_snapshot:
                if existing.status == "superseded" or existing.execution_snapshot[
                    "request_digest"
                ] != request_digest(
                    photo_id,
                    skill_id,
                    extra_prompt,
                    model,
                    package_options or PackageOptions(),
                ):
                    raise GenerationDomainError(
                        "idempotency_conflict", "请求已变化，请使用新的任务标识", 409
                    )
            metrics.record_generation_idempotency(outcome="hit")
            return existing

    photo = (
        await db.execute(
            select(Photo).where(Photo.id == photo_id, Photo.user_id == user_id)
        )
    ).scalar_one_or_none()
    if photo is None:
        raise GenerationDomainError("photo_not_found", "未找到这张照片", 404)

    skill: Skill | None = None
    selected_model = model or "wanx2.1-imageedit"
    if skill_id:
        skill = (
            await db.execute(select(Skill).where(Skill.id == skill_id))
        ).scalar_one_or_none()
        if skill is None:
            raise GenerationDomainError("skill_not_found", "未找到指定的 Skill", 404)
        if not (skill.is_official or skill.is_public or skill.owner_id == user_id):
            raise GenerationDomainError(
                "skill_forbidden", "没有权限使用这个 Skill", 403
            )
        selected_model = skill.model

    snapshot, frozen_inputs = None, []
    if skill is not None and getattr(skill, "kind", "template") == "package":
        try:
            snapshot, frozen_inputs = await build_snapshot(
                db,
                photo,
                skill,
                extra_prompt,
                model,
                package_options or PackageOptions(),
                planning_key=idempotency_key,
            )
        except GenerationDomainError:
            raise
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, PackageExecutionError)
                else "创作方案生成失败，请稍后重试"
            )
            raise GenerationDomainError("package_plan_failed", message, 422) from exc
        selected_model = snapshot["model"]
        # Same owner serializes prepare/confirm; only unconfirmed prior plans expire.
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
        if idempotency_key:
            concurrent = (
                await db.execute(
                    select(Generation).where(
                        Generation.user_id == user_id,
                        Generation.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if concurrent is not None:
                if (
                    concurrent.execution_snapshot
                    and concurrent.execution_snapshot["request_digest"]
                    == snapshot["request_digest"]
                    and concurrent.status != "superseded"
                ):
                    return concurrent
                raise GenerationDomainError(
                    "idempotency_conflict", "请求已变化，请使用新的任务标识", 409
                )
        await db.execute(
            update(Generation)
            .where(
                Generation.user_id == user_id,
                Generation.source_photo_id == photo_id,
                Generation.skill_id == skill_id,
                Generation.status == "awaiting_confirmation",
                Generation.execution_snapshot.is_not(None),
            )
            .values(status="superseded", confirmation_token=None)
        )

    now = datetime.now(timezone.utc)
    generation = Generation(
        user_id=user_id,
        source_photo_id=photo.id,
        skill_id=skill.id if skill else None,
        extra_prompt=extra_prompt,
        model=selected_model,
        status="awaiting_confirmation",
        estimated_cost_yuan=Decimal(str(settings.generation_estimated_cost_yuan)),
        execution_snapshot=snapshot,
        execution_digest=digest(snapshot) if snapshot else None,
        idempotency_key=idempotency_key,
        confirmation_token=uuid4(),
        confirmation_expires_at=now
        + timedelta(seconds=settings.generation_confirmation_ttl_seconds),
        enqueue_status="not_queued",
    )
    db.add(generation)
    if snapshot:
        generation.estimated_cost_yuan = Decimal(str(snapshot["estimated_cost_yuan"]))
        await db.flush()
        for position, content in enumerate(frozen_inputs):
            db.add(
                GenerationInput(
                    generation_id=generation.id,
                    position=position,
                    content=content,
                    media_type="image/png",
                )
            )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if not idempotency_key:
            raise
        existing = (
            await db.execute(
                select(Generation).where(
                    Generation.user_id == user_id,
                    Generation.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        metrics.record_generation_idempotency(outcome="race_hit")
        return existing
    await db.refresh(generation)
    metrics.record_generation_confirmation(
        variant=agent_variant_for_user(user_id), outcome="prepared"
    )
    return generation


async def _reserve_quota(db: AsyncSession, user_id: UUID) -> bool:
    today = date.today()
    stmt = (
        pg_insert(RateLimit)
        .values(user_id=user_id, day=today, gen_count=0, gen_reserved=1)
        .on_conflict_do_update(
            index_elements=["user_id", "day"],
            set_={"gen_reserved": RateLimit.gen_reserved + 1},
            where=(RateLimit.gen_count + RateLimit.gen_reserved)
            < settings.gen_daily_free_quota,
        )
        .returning(RateLimit.gen_reserved)
    )
    reserved = (await db.execute(stmt)).scalar_one_or_none()
    return reserved is not None


async def release_reserved_quota(db: AsyncSession, generation: Generation) -> None:
    if not generation.quota_reserved:
        return
    await db.execute(
        update(RateLimit)
        .where(
            RateLimit.user_id == generation.user_id,
            RateLimit.day == (generation.quota_reserved_day or date.today()),
            RateLimit.gen_reserved > 0,
        )
        .values(gen_reserved=RateLimit.gen_reserved - 1)
    )
    generation.quota_reserved = False
    generation.quota_reserved_day = None


async def consume_reserved_quota(db: AsyncSession, generation: Generation) -> None:
    if generation.quota_reserved:
        await db.execute(
            update(RateLimit)
            .where(
                RateLimit.user_id == generation.user_id,
                RateLimit.day == (generation.quota_reserved_day or date.today()),
                RateLimit.gen_reserved > 0,
            )
            .values(
                gen_reserved=RateLimit.gen_reserved - 1,
                gen_count=RateLimit.gen_count + 1,
            )
        )
        generation.quota_reserved = False
        generation.quota_reserved_day = None
        return
    # 兼容迁移前已创建且没有预占额度的任务。
    stmt = (
        pg_insert(RateLimit)
        .values(
            user_id=generation.user_id,
            day=date.today(),
            gen_count=1,
            gen_reserved=0,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "day"],
            set_={"gen_count": RateLimit.gen_count + 1},
        )
    )
    await db.execute(stmt)


async def confirm_generation(
    *,
    db: AsyncSession,
    user_id: UUID,
    generation_id: UUID,
    confirmation_token: UUID,
    execution_digest: str | None = None,
) -> Generation:
    """一次性确认并入队；重复确认返回同一任务，入队失败可安全重试。"""

    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    generation = (
        await db.execute(
            select(Generation)
            .where(
                and_(
                    Generation.id == generation_id,
                    Generation.user_id == user_id,
                )
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if generation is None:
        raise GenerationDomainError("generation_not_found", "生成任务不存在", 404)
    if generation.confirmation_token != confirmation_token:
        raise GenerationDomainError("confirmation_invalid", "生成确认已失效", 409)
    if (
        generation.execution_snapshot
        and execution_digest != generation.execution_digest
    ):
        raise GenerationDomainError(
            "snapshot_confirmation_required", "请核对当前创作方案后确认", 409
        )
    if generation.status in {"processing", "done"} or (
        generation.status == "pending" and generation.enqueue_status == "queued"
    ):
        return generation

    if generation.status == "awaiting_confirmation":
        if generation.execution_snapshot:
            try:
                await validate_execution(db, generation)
            except PackageExecutionError as exc:
                raise GenerationDomainError("snapshot_changed", str(exc), 409) from exc
        expires_at = generation.confirmation_expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                generation.status = "failed"
                generation.last_error_code = "confirmation_expired"
                await db.commit()
                metrics.record_generation_confirmation(
                    variant=agent_variant_for_user(user_id), outcome="expired"
                )
                raise GenerationDomainError(
                    "confirmation_expired", "生成确认已过期，请重新发起", 409
                )
        if not generation.quota_reserved:
            if not await _reserve_quota(db, user_id):
                await db.rollback()
                metrics.record_generation_confirmation(
                    variant=agent_variant_for_user(user_id), outcome="quota_rejected"
                )
                raise GenerationDomainError(
                    "quota_exceeded",
                    f"今日生成额度已用完（上限 {settings.gen_daily_free_quota} 次）",
                    429,
                )
            generation.quota_reserved = True
            generation.quota_reserved_day = date.today()
        generation.status = "pending"
        generation.progress_stage = "queued"

    if generation.status not in {"pending", "queue_failed"}:
        raise GenerationDomainError(
            "generation_state_invalid", f"当前状态不可确认：{generation.status}", 409
        )
    generation.enqueue_status = "enqueueing"

    from app.workers.tasks import enqueue_generate_photo

    try:
        queued = await enqueue_generate_photo(generation.id)
    except Exception as exc:  # noqa: BLE001
        queued = False
        generation.last_error_code = type(exc).__name__[:64]
    generation.enqueue_status = "queued" if queued else "failed"
    generation.status = "pending" if queued else "queue_failed"
    generation.progress_stage = "queued" if queued else "queue_failed"
    await db.commit()
    if not queued:
        metrics.record_generation_confirmation(
            variant=agent_variant_for_user(user_id), outcome="enqueue_failed"
        )
        raise GenerationDomainError(
            "generation_enqueue_failed",
            "生成任务暂未入队，保留原任务后可安全重试",
            503,
        )
    metrics.record_generation_confirmation(
        variant=agent_variant_for_user(user_id), outcome="confirmed"
    )
    return generation


def generation_confirmation_payload(generation: Generation) -> dict[str, Any]:
    return {
        "execution_digest": generation.execution_digest,
        "execution_snapshot": generation.execution_snapshot,
        "generation_id": str(generation.id),
        "confirmation_id": str(generation.confirmation_token),
        "photo_id": str(generation.source_photo_id),
        "skill_id": str(generation.skill_id) if generation.skill_id else None,
        "model": generation.model,
        "estimated_cost_yuan": float(generation.estimated_cost_yuan or 0),
        "expires_at": (
            generation.confirmation_expires_at.isoformat()
            if generation.confirmation_expires_at
            else None
        ),
    }
