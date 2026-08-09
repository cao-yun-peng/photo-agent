"""Skill 主动推荐服务.

推荐逻辑：
- 用户画像 skill_affinity：用户用过的 Skill 得分高；
- 标签匹配：基于当前照片/搜索的 objects、用户 tag_affinity，匹配 Skill 名称/描述/Prompt；
- 新鲜度：新上线/最近创建的官方 Skill 有额外加分；
- 热门度：use_count 做 log 平滑，避免头部垄断；
- 官方/公开 Skill 保底曝光。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.photo import Photo
from app.models.skill import Skill
from app.models.user_profile import UserProfile
from app.services.oss import sign_get_url

logger = logging.getLogger(__name__)

_FRESHNESS_HALF_LIFE_DAYS = 30.0


def _freshness_score(created_at: datetime | None) -> float:
    if created_at is None:
        return 0.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return math.exp(-age_days * math.log(2) / _FRESHNESS_HALF_LIFE_DAYS)


def _popularity_score(use_count: int) -> float:
    """use_count 做 log 平滑，0 次也有保底分。"""
    return math.log1p(use_count or 0) / 5.0  # 5 是一个经验参考值，可调整


def _keyword_match_score(skill: Skill, tags: set[str], weights: dict[str, float]) -> float:
    """简单关键词匹配：tag 出现在 Skill 名称/描述/Prompt 中即得分。"""
    text = " ".join(
        filter(
            None,
            [
                skill.name or "",
                skill.description or "",
                skill.prompt_template or "",
            ],
        )
    ).lower()
    score = 0.0
    for tag in tags:
        if tag and tag.lower() in text:
            score += weights.get(tag, 0.5)
    return min(1.0, score)  # 封顶避免单一项拉满


async def recommend_skills(
    db: AsyncSession,
    user_id: UUID,
    photo_ids: list[UUID] | None = None,
    limit: int = 5,
) -> list[dict]:
    """为用户推荐 Skill。

    Args:
        db: 数据库会话。
        user_id: 当前用户 ID。
        photo_ids: 可选上下文照片 ID（如搜索结果），用于提取标签做内容匹配。
        limit: 返回数量。

    Returns:
        推荐 Skill 列表，每项包含 id/name/description/cover_url/score/reason。
    """
    # 1. 读取画像
    profile = (
        await db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
    ).scalar_one_or_none()

    # 2. 读取可访问 Skill（官方 + 公开 + 自己创建）
    skills = (
        await db.execute(
            select(Skill).where(
                or_(
                    Skill.is_official.is_(True),
                    Skill.is_public.is_(True),
                    Skill.owner_id == user_id,
                )
            )
        )
    ).scalars().all()

    if not skills:
        return []

    # 3. 收集上下文标签与权重
    context_tags: set[str] = set()
    tag_weights: dict[str, float] = {}

    if profile and profile.tag_affinity:
        for tag, score in profile.tag_affinity.items():
            if tag:
                context_tags.add(tag)
                tag_weights[tag] = max(tag_weights.get(tag, 0.0), score)

    if photo_ids:
        photos = (
            await db.execute(
                select(Photo).where(
                    and_(
                        Photo.id.in_(photo_ids),
                        Photo.user_id == user_id,
                        Photo.ai_analysis.is_not(None),
                    )
                )
            )
        ).scalars().all()
        for photo in photos:
            analysis = photo.ai_analysis or {}
            for obj in analysis.get("objects", []):
                if obj:
                    context_tags.add(obj)
                    tag_weights[obj] = max(tag_weights.get(obj, 0.0), 0.6)
            for key in ("mood", "scene", "scene_detail"):
                val = analysis.get(key)
                if val:
                    context_tags.add(val)
                    tag_weights[val] = max(tag_weights.get(val, 0.0), 0.4)

    # 4. 打分
    scored: list[tuple[Skill, float, str]] = []
    for skill in skills:
        score = 0.0
        reasons: list[str] = []

        # 用户 Skill 偏好
        if profile and profile.skill_affinity:
            affinity = profile.skill_affinity.get(str(skill.id), 0.0)
            if affinity:
                score += affinity * 1.0
                reasons.append("常用 Skill")

        # 标签匹配
        if context_tags:
            tag_score = _keyword_match_score(skill, context_tags, tag_weights)
            if tag_score:
                score += tag_score * 0.8
                reasons.append("内容匹配")

        # 新鲜度
        fresh = _freshness_score(skill.created_at)
        if fresh > 0.5:
            score += fresh * 0.3
            reasons.append("新鲜上线")

        # 热门度
        score += _popularity_score(skill.use_count or 0) * 0.2

        # 官方/公开保底曝光
        if skill.is_official:
            score += 0.1
            reasons.append("官方推荐")

        scored.append((skill, round(score, 4), ", ".join(reasons) if reasons else "为你推荐"))

    scored.sort(key=lambda x: x[1], reverse=True)

    # 5. 组装输出
    result = []
    for skill, score, reason in scored[:limit]:
        result.append(
            {
                "id": str(skill.id),
                "name": skill.name,
                "description": skill.description,
                "cover_url": sign_get_url(skill.cover_key) if skill.cover_key else None,
                "is_official": skill.is_official,
                "score": score,
                "reason": reason,
            }
        )
    return result
