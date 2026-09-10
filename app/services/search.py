"""搜索相关工具：Embedding 缓存、混合评分、游标编解码。"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.search_budget import model_call
from app.models.photo import Photo
from app.models.user_profile import UserProfile
from app.services.ai import embed_query

logger = logging.getLogger(__name__)


def infer_complete_result_filters(query: str) -> dict[str, object]:
    """为可可靠映射到现有 VL 字段的全量查询生成硬过滤条件。

    这里只收录高置信度映射，避免为了“完整”而错误缩小普通自然语言查询。
    未命中的概念仍由向量召回排序，但不会伪造结构化条件。
    """

    normalized = "".join(str(query or "").split())
    photo_types: list[str] = []
    is_selfie: bool | None = None
    people_count_min: int | None = None
    people_count_max: int | None = None
    selfie_negated = any(
        word in normalized for word in ("不是自拍", "不要自拍", "非自拍", "除了自拍")
    )
    if "自拍" in normalized and not selfie_negated:
        photo_types.append("selfie")
        is_selfie = True
    elif selfie_negated:
        is_selfie = False
    if "截图" in normalized or "屏幕截图" in normalized:
        photo_types.append("screenshot")
    if any(word in normalized for word in ("合照", "合影", "团体照", "集体照")):
        photo_types.append("group_photo")
        people_count_min = 2
    if any(word in normalized for word in ("单人照", "个人照", "人像照")):
        photo_types.append("portrait")
        people_count_min = 1
        people_count_max = 1
    remainder = normalized
    for marker in (
        "把",
        "请",
        "帮我",
        "都给我",
        "给我",
        "全部",
        "所有",
        "全都",
        "一张不漏",
        "照片",
        "图片",
        "相片",
        "自拍",
        "手机截图",
        "屏幕截图",
        "截图",
        "合照",
        "合影",
        "团体照",
        "集体照",
        "单人照",
        "个人照",
        "人像照",
        "我自己选",
        "我来选",
        "让我选",
        "由我选",
        "自己选择",
        "从中选择",
    ):
        remainder = remainder.replace(marker, "")
    return {
        "photo_types": list(dict.fromkeys(photo_types)),
        "is_selfie": is_selfie,
        "people_count_min": people_count_min,
        "people_count_max": people_count_max,
        "all_album": not photo_types
        and is_selfie is None
        and people_count_min is None
        and people_count_max is None
        and not remainder.strip("，。！？!?、"),
    }


def complete_scope_is_reliable(
    inferred_filters: dict[str, object],
    *,
    tags: list[str] | None = None,
    scene: str | None = None,
    objects: list[str] | None = None,
    text_in_image: list[str] | None = None,
    mood: str | None = None,
    colors: list[str] | None = None,
    photo_types: list[str] | None = None,
    is_selfie: bool | None = None,
    people_count_min: int | None = None,
    people_count_max: int | None = None,
) -> bool:
    """完整集合必须有可在数据库中穷举的目标条件。

    时间范围只能限制相册窗口，不能证明“鸟/动物”等开放目标已经被过滤，
    因而不能单独作为完整语义范围。
    """

    structured = bool(
        inferred_filters.get("photo_types")
        or inferred_filters.get("is_selfie") is not None
        or inferred_filters.get("people_count_min") is not None
        or inferred_filters.get("people_count_max") is not None
        or photo_types
        or is_selfie is not None
        or people_count_min is not None
        or people_count_max is not None
    )
    explicit_semantic_filter = any(
        value for value in (tags, scene, objects, text_in_image, mood, colors)
    )
    return bool(
        structured or inferred_filters.get("all_album") or explicit_semantic_filter
    )


def resolve_semantic_threshold(
    requested: float | None,
    *,
    structured_collection: bool = False,
) -> tuple[float | None, str | None]:
    """解析相似度阈值；精确集合过滤不让向量分数误删结果。"""

    if structured_collection:
        return None, "structured_collection_filter"
    value = settings.search_semantic_min_score if requested is None else requested
    value = float(value or 0.0)
    if value <= 0:
        return None, "disabled"
    return min(value, 1.0), None


def apply_semantic_threshold(scored, threshold: float | None):
    """对 ``(photo, semantic, ...)`` 候选应用阈值并返回过滤数量。"""

    if threshold is None:
        return list(scored), 0
    kept = [item for item in scored if float(item[1]) >= threshold]
    return kept, len(scored) - len(kept)


def build_search_coverage_hint(
    coverage: dict[str, object] | None,
    *,
    requires_facets: bool,
    threshold: float | None,
    threshold_filtered_count: int,
) -> str | None:
    """生成用户可理解的覆盖范围提示，不声称模型看过未索引照片。"""

    messages: list[str] = []
    coverage = coverage or {}
    if requires_facets and not coverage.get("semantic_complete", True):
        semantic_message = coverage.get("semantic_message")
        if semantic_message:
            messages.append(str(semantic_message))
    if not coverage.get("complete", True) and coverage.get("message"):
        messages.append(str(coverage["message"]))
    if threshold is not None:
        messages.append(
            f"已应用相似度阈值 {threshold:.2f}，过滤 {threshold_filtered_count} 张低相似候选"
        )
    return "；".join(messages) or None


_EMB_TTL = 3600


async def get_query_embedding(text: str) -> tuple[list[float], bool]:
    from app.services.search_cache import cache_key, cached_call
    from app.services.ai import _EMB_URL, _is_mock

    key = cache_key(
        "embedding",
        [
            _EMB_URL,
            settings.qwen_embedding_model,
            1024,
            "query",
            text.strip(),
            _is_mock(),
        ],
    )

    def validate(value):
        if (
            not isinstance(value, list)
            or len(value) != 1024
            or any(
                not isinstance(x, (int, float)) or not math.isfinite(x) for x in value
            )
        ):
            raise ValueError("invalid embedding cache")
        return value

    return await cached_call(
        key,
        lambda: model_call("embedding", lambda: embed_query(text)),
        ttl=_EMB_TTL,
        validate=validate,
    )


# ------------------------------------------------------------------
# 混合评分
# ------------------------------------------------------------------
def recency_score(
    taken_at: datetime | None,
    half_life_days: float = 30.0,
    *,
    now: datetime | None = None,
) -> float:
    """
    时间新鲜度：越接近今天分数越高，指数衰减，半衰期 30 天。
    photos 没拍摄时间的按 0.3 计（保底，不至于完全沉底）。
    """
    if taken_at is None:
        return 0.3
    now = now or datetime.now(timezone.utc)
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
    return round((sem * w_sem + rec * w_rec + interaction * w_int) / total, 8)


# ------------------------------------------------------------------
# 游标编解码
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
    if not profile or not profile.style_distribution or not photo.embedding:
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
