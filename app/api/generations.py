"""生成相关路由：准备、确认、查询生成任务。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.generation import Generation
from app.models.user import User
from app.schemas.skill import (
    GenerateRequest,
    GenerationConfirmRequest,
    GenerationOut,
)
from app.services.generation_service import (
    GenerationDomainError,
    confirm_generation,
    prepare_generation,
)
from app.services.oss import sign_get_url
from app.services.rollout import agent_variant_for_user

router = APIRouter()


def _to_out(g: Generation) -> GenerationOut:
    return GenerationOut(
        id=g.id,
        source_photo_id=g.source_photo_id,
        skill_id=g.skill_id,
        extra_prompt=g.extra_prompt,
        result_oss_key=g.result_oss_key,
        result_url=sign_get_url(g.result_oss_key) if g.result_oss_key else None,
        status=g.status,
        error_message=g.error_message,
        model=g.model,
        cost_yuan=g.cost_yuan,
        estimated_cost_yuan=g.estimated_cost_yuan,
        confirmation_token=g.confirmation_token,
        confirmation_expires_at=g.confirmation_expires_at,
        enqueue_status=g.enqueue_status,
        attempt_count=g.attempt_count,
        created_at=g.created_at,
    )


def _raise_domain_error(exc: GenerationDomainError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.post(
    "/photos/{photo_id}/generate",
    response_model=GenerationOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="用 Skill 对某张照片做 AI 改造（异步）",
)
async def create_generation(
    photo_id: UUID,
    payload: GenerateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    try:
        gen = await prepare_generation(
            db=db,
            user_id=current_user.id,
            photo_id=photo_id,
            skill_id=payload.skill_id,
            extra_prompt=payload.extra_prompt,
            model=payload.model,
            idempotency_key=payload.idempotency_key,
        )
        # 控制组保留旧的一步式体验；v2 灰度组必须显式确认。
        if agent_variant_for_user(current_user.id) == "control" and gen.status in {
            "awaiting_confirmation",
            "queue_failed",
        }:
            gen = await confirm_generation(
                db=db,
                user_id=current_user.id,
                generation_id=gen.id,
                confirmation_token=gen.confirmation_token,
            )
    except GenerationDomainError as exc:
        _raise_domain_error(exc)
    return _to_out(gen)


@router.post(
    "/generations/{generation_id}/confirm",
    response_model=GenerationOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="确认并入队生成任务（可幂等重试）",
)
async def confirm_generation_route(
    generation_id: UUID,
    payload: GenerationConfirmRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    try:
        generation = await confirm_generation(
            db=db,
            user_id=current_user.id,
            generation_id=generation_id,
            confirmation_token=payload.confirmation_token,
        )
    except GenerationDomainError as exc:
        _raise_domain_error(exc)
    return _to_out(generation)


@router.get(
    "/generations",
    response_model=list[GenerationOut],
    summary="我的生成历史",
)
async def list_my_generations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[GenerationOut]:
    result = await db.execute(
        select(Generation)
        .where(Generation.user_id == current_user.id)
        .order_by(desc(Generation.created_at))
        .limit(limit)
        .offset(offset)
    )
    return [_to_out(g) for g in result.scalars().all()]


@router.get(
    "/generations/{generation_id}",
    response_model=GenerationOut,
    summary="生成任务详情（用于轮询状态）",
)
async def get_generation(
    generation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenerationOut:
    g = (
        await db.execute(
            select(Generation).where(
                and_(
                    Generation.id == generation_id,
                    Generation.user_id == current_user.id,
                )
            )
        )
    ).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    return _to_out(g)
