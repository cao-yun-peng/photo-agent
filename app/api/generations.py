"""生成相关路由：发起生成 + 生成历史."""
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import get_current_user
from app.database import get_db
from app.models.generation import Generation
from app.models.photo import Photo
from app.models.rate_limit import RateLimit
from app.models.skill import Skill
from app.models.user import User
from app.schemas.skill import GenerateRequest, GenerationOut
from app.services.oss import sign_get_url
from app.workers.tasks import enqueue_generate_photo

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
        created_at=g.created_at,
    )


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
    # 1) 源照片权限校验
    photo = (
        await db.execute(
            select(Photo).where(
                and_(Photo.id == photo_id, Photo.user_id == current_user.id)
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    # 2) Skill 校验
    skill = None
    model = payload.model or "wanx2.1-imageedit"
    if payload.skill_id:
        skill = (
            await db.execute(select(Skill).where(Skill.id == payload.skill_id))
        ).scalar_one_or_none()
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        if not (skill.is_official or skill.is_public or skill.owner_id == current_user.id):
            raise HTTPException(status_code=403, detail="Skill not accessible")
        model = skill.model

    # 3) 每日配额检查
    today = date.today()
    rl = (
        await db.execute(
            select(RateLimit).where(
                and_(RateLimit.user_id == current_user.id, RateLimit.day == today)
            )
        )
    ).scalar_one_or_none()
    used = rl.gen_count if rl else 0
    if used >= settings.gen_daily_free_quota:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"今日免费额度已用完（{used}/{settings.gen_daily_free_quota}），"
                f"明天再来或订阅解锁。"
            ),
        )

    # 4) 落库
    gen = Generation(
        user_id=current_user.id,
        source_photo_id=photo.id,
        skill_id=skill.id if skill else None,
        extra_prompt=payload.extra_prompt,
        model=model,
        status="pending",
    )
    db.add(gen)
    await db.commit()
    await db.refresh(gen)

    # 5) 入队
    await enqueue_generate_photo(gen.id)

    return _to_out(gen)


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
                and_(Generation.id == generation_id, Generation.user_id == current_user.id)
            )
        )
    ).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    return _to_out(g)
