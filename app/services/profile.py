"""用户画像聚合服务 — 从 UserEvent 计算偏好向量.

聚合策略：
- 只读取最近 lookback_days 天的事件，避免冷启动前的噪声；
- 按事件距今天数做指数衰减（半衰期 30 天），最近行为权重更高；
- Skill 偏好：generation_complete > skill_browse，失败生成降权；
- 标签亲和度：从生成源照片、点击/交互的照片中提取 objects / tags；
- 风格分布：对生成/点击/交互过的照片 embedding 做加权平均，与搜索向量对齐。
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.photo import Photo
from app.models.tag import PhotoTag, Tag
from app.models.user_event import UserEvent
from app.models.user_profile import UserProfile

logger = logging.getLogger(__name__)

_PROFILE_HALF_LIFE_DAYS = 30.0
_PROFILE_LOOKBACK_DAYS = 90


def _decay_weight(event_at: datetime, now: datetime | None = None) -> float:
    """基于时间衰减的权重，越近越高；半衰期 _PROFILE_HALF_LIFE_DAYS 天后权重减半。"""
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    delta_days = (now - event_at).total_seconds() / 86400.0
    if delta_days < 0:
        delta_days = 0.0
    return math.exp(-delta_days * math.log(2) / _PROFILE_HALF_LIFE_DAYS)


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    """把分数归一化到最大值为 1。空字典返回空。"""
    if not scores:
        return {}
    max_score = max(scores.values())
    if max_score <= 1e-9:
        return {k: 0.0 for k in scores}
    return {k: round(v / max_score, 4) for k, v in scores.items()}


async def build_user_profile(
    user_id: UUID,
    db: AsyncSession,
    lookback_days: int = _PROFILE_LOOKBACK_DAYS,
) -> UserProfile:
    """为指定用户重建画像。返回已 flush 但未 commit 的 UserProfile 对象。"""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=lookback_days)

    events = (
        await db.execute(
            select(UserEvent)
            .where(
                and_(
                    UserEvent.user_id == user_id,
                    UserEvent.created_at >= since,
                    UserEvent.event_type.in_(
                        ["generation_complete", "search_click", "skill_browse", "photo_interact"]
                    ),
                )
            )
            .order_by(UserEvent.created_at)
        )
    ).scalars().all()

    skill_scores: dict[str, float] = defaultdict(float)
    tag_scores: dict[str, float] = defaultdict(float)

    # photo_id -> 累计权重，用于风格向量聚合
    photo_weights: dict[UUID, float] = defaultdict(float)

    total_generations = 0
    total_searches = 0

    for evt in events:
        weight = _decay_weight(evt.created_at, now)
        payload = evt.payload or {}

        if evt.event_type == "generation_complete":
            total_generations += 1
            skill_id = payload.get("skill_id")
            status = payload.get("status", "done")

            # 失败生成只给 20% 权重，避免模型/网络问题污染偏好
            w = weight * (0.2 if status != "done" else 1.0)
            if skill_id:
                skill_scores[skill_id] += w * 2.0  # 生成比浏览强

            for tag in payload.get("photo_tags", []):
                if tag:
                    tag_scores[str(tag)] += w

            photo_id = payload.get("source_photo_id")
            if photo_id:
                try:
                    pid = UUID(photo_id)
                    photo_weights[pid] += w
                except ValueError:
                    pass

        elif evt.event_type == "skill_browse":
            skill_id = payload.get("skill_id")
            if skill_id:
                skill_scores[skill_id] += weight * 0.3

        elif evt.event_type == "search_click":
            total_searches += 1
            photo_id = payload.get("photo_id")
            if photo_id:
                try:
                    pid = UUID(photo_id)
                    photo_weights[pid] += weight * 1.0
                except ValueError:
                    pass

        elif evt.event_type == "photo_interact":
            action = payload.get("action", "view")
            action_w = 1.0 if action in ("favorite", "download") else 0.5
            photo_id = payload.get("photo_id")
            if photo_id:
                try:
                    pid = UUID(photo_id)
                    photo_weights[pid] += weight * action_w
                except ValueError:
                    pass

    # 补充 photo_tags 里的标签：对有权重的照片查询 AI objects 和用户标签
    tag_scores = await _enrich_tag_scores(db, user_id, photo_weights, tag_scores)

    # 风格向量：对有权重的照片 embedding 做加权平均
    style_distribution = await _build_style_vector(db, user_id, photo_weights)

    skill_affinity = _normalize_scores(dict(skill_scores))
    tag_affinity = _normalize_scores(dict(tag_scores))

    # upsert UserProfile
    profile = (
        await db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = UserProfile(user_id=user_id)
        db.add(profile)

    profile.skill_affinity = skill_affinity
    profile.tag_affinity = tag_affinity
    profile.style_distribution = style_distribution
    profile.total_generations = total_generations
    profile.total_searches = total_searches

    await db.flush()
    logger.info(
        "profile rebuilt | user=%s events=%d skills=%d tags=%d photos=%d",
        user_id,
        len(events),
        len(skill_affinity),
        len(tag_affinity),
        len(photo_weights),
    )
    return profile


async def _enrich_tag_scores(
    db: AsyncSession,
    user_id: UUID,
    photo_weights: dict[UUID, float],
    tag_scores: dict[str, float],
) -> dict[str, float]:
    """根据 photo_weights 中的照片，补充 objects 和用户手动标签的分数。"""
    if not photo_weights:
        return tag_scores

    photo_ids = list(photo_weights.keys())

    # AI 分析出的 objects
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
        w = photo_weights.get(photo.id, 0.0)
        analysis = photo.ai_analysis or {}
        for obj in analysis.get("objects", []):
            if obj:
                tag_scores[str(obj)] += w * 0.5
        for mood in [analysis.get("mood"), analysis.get("scene")]:
            if mood:
                tag_scores[str(mood)] += w * 0.3

    # 用户手动标签
    rows = (
        await db.execute(
            select(PhotoTag.photo_id, Tag.name)
            .join(Tag, PhotoTag.tag_id == Tag.id)
            .where(
                and_(
                    PhotoTag.photo_id.in_(photo_ids),
                    Tag.user_id == user_id,
                )
            )
        )
    ).all()

    for photo_id, name in rows:
        w = photo_weights.get(photo_id, 0.0)
        if name:
            tag_scores[name] += w * 0.8

    return tag_scores


async def _build_style_vector(
    db: AsyncSession,
    user_id: UUID,
    photo_weights: dict[UUID, float],
) -> list[float] | None:
    """对 photo_weights 中照片的 embedding 做加权平均。"""
    if not photo_weights:
        return None

    photo_ids = list(photo_weights.keys())
    rows = (
        await db.execute(
            select(Photo.id, Photo.embedding)
            .where(
                and_(
                    Photo.id.in_(photo_ids),
                    Photo.user_id == user_id,
                    Photo.embedding.is_not(None),
                )
            )
        )
    ).all()

    if not rows:
        return None

    dim = None
    weighted_sum: list[float] | None = None
    total_weight = 0.0

    for photo_id, embedding in rows:
        w = photo_weights.get(photo_id, 0.0)
        if not w or not embedding:
            continue
        if dim is None:
            dim = len(embedding)
            weighted_sum = [0.0] * dim
        if len(embedding) != dim or weighted_sum is None:
            continue
        for i, v in enumerate(embedding):
            weighted_sum[i] += float(v) * w
        total_weight += w

    if not weighted_sum or total_weight <= 1e-9:
        return None

    mean_vec = [round(v / total_weight, 6) for v in weighted_sum]
    # 做 L2 归一化，保证与向量检索的 cosine_distance 可比
    norm = sum(v * v for v in mean_vec) ** 0.5 or 1.0
    return [v / norm for v in mean_vec]
