"""Skill 增删改查 + 广场."""
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import get_current_user
from app.database import get_db
from app.models.rate_limit import RateLimit
from app.models.skill import Skill
from app.models.user import User
from app.schemas.skill import (
    QuotaInfo,
    SkillCreate,
    SkillOut,
    SkillUpdate,
)
from app.services.events import log_event
from app.services.oss import sign_get_url

router = APIRouter()


def _to_out(sk: Skill) -> SkillOut:
    return SkillOut(
        id=sk.id,
        owner_id=sk.owner_id,
        name=sk.name,
        description=sk.description,
        prompt_template=sk.prompt_template,
        reference_keys=sk.reference_keys or [],
        cover_key=sk.cover_key,
        cover_url=sign_get_url(sk.cover_key) if sk.cover_key else None,
        model=sk.model,
        function=sk.function,
        strength=sk.strength,
        is_public=sk.is_public,
        is_official=sk.is_official,
        use_count=sk.use_count,
        created_at=sk.created_at,
    )


# ------------------------------------------------------------------
# 列表
# ------------------------------------------------------------------
@router.get(
    "",
    response_model=list[SkillOut],
    summary="我的 Skill 列表（含官方 + 我自建）",
)
async def list_my_skills(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SkillOut]:
    stmt = (
        select(Skill)
        .where(or_(Skill.is_official.is_(True), Skill.owner_id == current_user.id))
        .order_by(desc(Skill.is_official), desc(Skill.created_at))
    )
    result = await db.execute(stmt)
    return [_to_out(sk) for sk in result.scalars().all()]


@router.get(
    "/plaza",
    response_model=list[SkillOut],
    summary="广场：公开的用户 Skill + 官方 Skill",
)
async def plaza(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[SkillOut]:
    stmt = (
        select(Skill)
        .where(or_(Skill.is_official.is_(True), Skill.is_public.is_(True)))
        .order_by(desc(Skill.is_official), desc(Skill.use_count), desc(Skill.created_at))
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return [_to_out(sk) for sk in result.scalars().all()]


@router.get(
    "/{skill_id}",
    response_model=SkillOut,
    summary="Skill 详情",
)
async def get_skill(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillOut:
    sk = (await db.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
    if sk is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    # 权限：官方 or 公开 or 自己的
    if not (sk.is_official or sk.is_public or sk.owner_id == current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")

    await log_event(
        user_id=current_user.id,
        event_type="skill_browse",
        payload={
            "skill_id": str(sk.id),
            "is_official": sk.is_official,
            "is_public": sk.is_public,
            "source": "detail",
        },
    )
    return _to_out(sk)


# ------------------------------------------------------------------
# 增删改
# ------------------------------------------------------------------
@router.post(
    "",
    response_model=SkillOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建自定义 Skill",
)
async def create_skill(
    payload: SkillCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillOut:
    sk = Skill(
        owner_id=current_user.id,
        name=payload.name,
        description=payload.description,
        prompt_template=payload.prompt_template,
        reference_keys=payload.reference_keys,
        cover_key=payload.cover_key,
        model=payload.model,
        function=payload.function,
        strength=payload.strength,
        is_public=payload.is_public,
        is_official=False,
    )
    db.add(sk)
    await db.commit()
    await db.refresh(sk)
    return _to_out(sk)


@router.patch(
    "/{skill_id}",
    response_model=SkillOut,
    summary="修改我的 Skill",
)
async def update_skill(
    skill_id: UUID,
    payload: SkillUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillOut:
    sk = (await db.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
    if sk is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if sk.is_official or sk.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot modify this skill")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(sk, field, value)
    await db.commit()
    await db.refresh(sk)
    return _to_out(sk)


@router.delete(
    "/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除我的 Skill",
)
async def delete_skill(
    skill_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    sk = (await db.execute(select(Skill).where(Skill.id == skill_id))).scalar_one_or_none()
    if sk is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    if sk.is_official or sk.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete this skill")
    await db.delete(sk)
    await db.commit()


# ------------------------------------------------------------------
# 配额查询
# ------------------------------------------------------------------
@router.get(
    "/_/quota",
    response_model=QuotaInfo,
    summary="今日剩余免费生成次数",
)
async def my_quota(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QuotaInfo:
    today = date.today()
    row = (
        await db.execute(
            select(RateLimit).where(
                and_(RateLimit.user_id == current_user.id, RateLimit.day == today)
            )
        )
    ).scalar_one_or_none()
    used = row.gen_count if row else 0
    quota = settings.gen_daily_free_quota
    return QuotaInfo(used=used, quota=quota, remaining=max(0, quota - used))
