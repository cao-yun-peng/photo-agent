"""搜索相关工具：Embedding 缓存、混合评分、游标编解码。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.photo import Photo
from app.models.user_profile import UserProfile
from app.services.ai import embed_query

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Redis 连接（懒加载单例）
# ------------------------------------------------------------------
_redis: Redis | None = None


async def _get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# ------------------------------------------------------------------
# Embedding 缓存
# ------------------------------------------------------------------
_EMB_TTL = 24 * 3600
_EMB_PREFIX = "emb:q:"


def _emb_key(text: str) -> str:
    """按查询文本的 sha1 存 key，跨用户共享（Embedding 与用户无关）。"""
    digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()
    return f"{_EMB_PREFIX}{digest}"


async def get_query_embedding(text: str) -> tuple[list[float], bool]:
    """
    返回 (向量, 是否命中缓存)。
    - 命中 → 直接返回 Redis 里的值
    - 未命中 → 调 DashScope，写回缓存

    Redis 读容错：Redis 不可用时降级为 cache miss，不阻断搜索。
    """
    key = _emb_key(text)
    try:
        r = await _get_redis()
        hit = await r.get(key)
        if hit:
            try:
                return json.loads(hit), True
            except json.JSONDecodeError:
                # 数据损坏就当作未命中
                await r.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache read failed, treating as miss: %s", exc)

    vec = await embed_query(text)
    try:
        r = await _get_redis()
        await r.setex(key, _EMB_TTL, json.dumps(vec))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache write failed: %s", exc)
    return vec, False


# ------------------------------------------------------------------
# 混合评分
# ------------------------------------------------------------------
def recency_score(taken_at: datetime | None, half_life_days: float = 30.0) -> float:
    """
    时间新鲜度：越接近今天分数越高，指数衰减，半衰期 30 天。
    photos 没拍摄时间的按 0.3 计（保底，不至于完全沉底）。
    """
    if taken_at is None:
        return 0.3
    now = datetime.now(timezone.utc)
    delta = (now - taken_at).total_seconds() / 86400.0
    if delta < 0:
        # 未来时间（EXIF 异常），当作今天
        delta = 0
    return math.exp(-delta / half_life_days)


def semantic_score(cosine_distance: float) -> float:
    """
    pgvector 的 <=> 是余弦距离（0–2 之间，0 最相似）。
    转成 0–1 的分数：1 - dist / 2。
    """
    return max(0.0, 1.0 - cosine_distance / 2.0)


def combine(
    sem: float,
    rec: float,
    interaction: float,
    w_sem: float,
    w_rec: float,
    w_int: float,
) -> float:
    """加权求和；权重会归一。"""
    total = max(w_sem + w_rec + w_int, 1e-6)
    return (sem * w_sem + rec * w_rec + interaction * w_int) / total


# ------------------------------------------------------------------
# 游标编解码
# ------------------------------------------------------------------
def encode_cursor(final_score: float, photo_id: UUID) -> str:
    raw = f"{final_score:.8f}|{photo_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[float, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        score_str, pid = raw.split("|", 1)
        return float(score_str), pid
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------
# 个性化交互分 s_int
# ------------------------------------------------------------------
async def get_user_profile(
    db: AsyncSession,
    user_id: UUID,
) -> UserProfile | None:
    """读取用户画像；没有则返回 None（走默认排序）。"""
    return (
        await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    ).scalar_one_or_none()


def _tag_affinity_score(profile: UserProfile | None, photo: Photo) -> float:
    """根据用户标签亲和度给照片打分（0–1）。"""
    if not profile or not profile.tag_affinity:
        return 0.0
    tags: set[str] = set()
    analysis = photo.ai_analysis or {}
    tags.update(analysis.get("objects", []))
    if analysis.get("mood"):
        tags.add(analysis["mood"])
    if analysis.get("scene"):
        tags.add(analysis["scene"])
    if not tags:
        return 0.0

    matched = sum(profile.tag_affinity.get(t, 0.0) for t in tags if t)
    # 用 soft 压缩到 0–1，避免单个高匹配直接拉满
    return round(min(1.0, matched / (1.0 + matched)), 4)


def _style_similarity(profile: UserProfile | None, photo: Photo) -> float:
    """计算照片 embedding 与用户风格向量的余弦相似度（0–1）。"""
    if (
        not profile
        or not profile.style_distribution
        or not photo.embedding
    ):
        return 0.0

    a = profile.style_distribution
    b = photo.embedding
    if len(a) != len(b):
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def personalized_interaction_score(
    profile: UserProfile | None,
    photo: Photo,
    w_tag: float = 0.6,
    w_style: float = 0.4,
) -> float:
    """综合标签亲和与风格相似，输出 0–1 的个性化交互分 s_int。"""
    s_tag = _tag_affinity_score(profile, photo)
    s_style = _style_similarity(profile, photo)
    total = w_tag + w_style
    return round((s_tag * w_tag + s_style * w_style) / total, 4)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦距离（pgvector <=> 的等价实现）。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return 2.0
    return 1.0 - dot / (norm_a * norm_b)


async def smart_album_fallback(
    db: AsyncSession,
    user_id: UUID,
    query: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    w_semantic: float = 0.4,
    w_recency: float = 0.35,
    w_interaction: float = 0.25,
) -> tuple[list[tuple[Photo, float, float, float, float]], str | None]:
    """智能全量相册兜底：对用户全部照片按语义+新鲜度+个性化综合排序。

    Args:
        db: 数据库会话。
        user_id: 用户 ID。
        query: 可选查询文本；提供时按语义相似度排序，否则主要按新鲜度+个性化。
        limit: 每页数量。
        cursor: 复合游标。
        w_semantic, w_recency, w_interaction: 排序权重。

    Returns:
        (排序后的照片及分数列表, next_cursor)
    """
    profile = await get_user_profile(db, user_id)
    query_vec: list[float] | None = None
    if query:
        query_vec, _ = await get_query_embedding(query)

    stmt = select(Photo).where(Photo.user_id == user_id)
    result = await db.execute(stmt)
    photos = result.scalars().all()

    scored: list[tuple[Photo, float, float, float, float]] = []
    for photo in photos:
        s_sem = 0.0
        if query_vec and photo.embedding:
            s_sem = semantic_score(_cosine_distance(photo.embedding, query_vec))
        s_rec = recency_score(photo.taken_at)
        s_int = personalized_interaction_score(profile, photo)
        final = combine(s_sem, s_rec, s_int, w_semantic, w_recency, w_interaction)
        scored.append((photo, round(s_sem, 4), round(s_rec, 4), round(s_int, 4), round(final, 4)))

    scored.sort(key=lambda x: x[4], reverse=True)

    if cursor:
        parsed_cursor = decode_cursor(cursor)
        if parsed_cursor is not None:
            cur_score, cur_id = parsed_cursor
            scored = [
                (p, sem, rec, inter, fin)
                for (p, sem, rec, inter, fin) in scored
                if fin < cur_score or (abs(fin - cur_score) < 1e-9 and str(p.id) > cur_id)
            ]

    page = scored[:limit]
    next_cursor = None
    if len(page) == limit and len(scored) > limit:
        last_p, _, _, _, last_score = page[-1]
        next_cursor = encode_cursor(last_score, last_p.id)

    return page, next_cursor
