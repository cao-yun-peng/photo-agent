"""Agent 可调用的 Tool 封装层。

设计原则：
- 每个 Tool 都是纯函数/协程，签名清晰，返回值可 JSON 序列化；
- Tool 内部处理权限、异常和兜底，不让 Agent 核心关心业务细节；
- Tool 返回统一 dict，包含 ok / data / error / hint 字段，方便 Agent 做下一步决策。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.generation import Generation
from app.models.photo import Photo
from app.models.rate_limit import RateLimit
from app.models.skill import Skill
from app.models.tag import PhotoTag, Tag
from app.schemas.photo import ParsedQuery
from app.services.oss import sign_get_url
from app.services.query_parser import parse_query, resolve_auto_parsed_query
from app.services.search_constraints import (
    extract_structured_constraints,
    validate_scored_candidates,
)
from app.services.search_reranker import rerank_scored_candidates
from app.services.recommend import recommend_skills
from app.services.search import (
    combine,
    decode_cursor,
    encode_cursor,
    get_query_embedding,
    get_user_profile,
    personalized_interaction_score,
    recency_score,
    semantic_score,
)
from app.workers.tasks import enqueue_generate_photo

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# P1-4: 错误分类系统
# 让 Agent 能根据 error_type 做不同处理：重试/换路径/提示用户/放弃
# ------------------------------------------------------------------
class ToolError(Exception):
    """工具错误基类，携带 error_type 供 Agent 决策。"""

    def __init__(self, message: str, error_type: str = "unknown") -> None:
        self.error_type = error_type
        super().__init__(message)


class RetryableError(ToolError):
    """可重试错误：DB 连接超时、网络抖动等临时故障。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "retryable")


class UserFixableError(ToolError):
    """用户可修复错误：参数无效、权限不足、额度不足等。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "user_fixable")


class PermanentError(ToolError):
    """永久错误：数据不一致、约束冲突等不可恢复的故障。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "permanent")


def _classify_exception(exc: Exception) -> str:
    """将原始异常映射为 error_type 字符串。"""
    # SQLAlchemy 运维错误（连接断开、超时）-> 可重试
    try:
        from sqlalchemy.exc import OperationalError

        if isinstance(exc, OperationalError):
            return "retryable"
    except ImportError:
        pass

    # 数据完整性冲突 -> 永久
    try:
        from sqlalchemy.exc import IntegrityError

        if isinstance(exc, IntegrityError):
            return "permanent"
    except ImportError:
        pass

    # 值错误/类型错误 -> 用户可修复
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "user_fixable"

    # 权限相关 -> 用户可修复
    if isinstance(exc, PermissionError):
        return "user_fixable"

    # ToolError 子类自带 error_type
    if isinstance(exc, ToolError):
        return exc.error_type

    # 其他未知错误
    return "unknown"


# ------------------------------------------------------------------
# 公共辅助
# ------------------------------------------------------------------
def _photo_to_search_item(
    photo: Photo,
    sem: float,
    rec: float,
    inter: float,
    final: float,
) -> dict:
    """把 Photo ORM 对象转成 Agent 可消费的 dict。"""
    return {
        "id": str(photo.id),
        "thumb_url": sign_get_url(photo.thumb_key or photo.oss_key),
        "taken_at": photo.taken_at.isoformat() if photo.taken_at else None,
        "ai_description": photo.ai_description,
        "status": photo.status,
        "ai_analysis": photo.ai_analysis or {},
        "score_semantic": round(sem, 4),
        "score_recency": round(rec, 4),
        "score_interaction": round(inter, 4),
        "score_final": round(final, 4),
    }


async def _check_generate_quota(
    db: AsyncSession, user_id: UUID
) -> tuple[bool, int, int]:
    """检查用户今日生成额度。返回 (是否可用, 已用, 总额)."""
    today = datetime.now(tz=timezone.utc).date()
    rl = (
        await db.execute(
            select(RateLimit).where(
                and_(RateLimit.user_id == user_id, RateLimit.day == today)
            )
        )
    ).scalar_one_or_none()
    used = rl.gen_count if rl else 0
    quota = settings.gen_daily_free_quota
    return used < quota, used, quota


# ------------------------------------------------------------------
# Tool 1: 搜索照片
# ------------------------------------------------------------------
async def search_photos(
    *,
    user_id: UUID,
    db: AsyncSession,
    query: str,
    from_date: date | None = None,
    to_date: date | None = None,
    tags: list[str] | None = None,
    scene: str | None = None,
    objects: list[str] | None = None,
    text_in_image: list[str] | None = None,
    mood: str | None = None,
    colors: list[str] | None = None,
    status: str | None = "done",
    limit: int = 10,
    cursor: str | None = None,
    auto_parse: bool = True,
    verify_constraints: bool = True,
    verify_semantic: bool = True,
    w_semantic: float = 0.7,
    w_recency: float = 0.2,
    w_interaction: float = 0.1,
) -> dict:
    """Agent 搜索 Tool：自然语言 + 结构化过滤 + 混合排序。

    返回：
        {
          "ok": bool,
          "items": [ {...}, ... ],
          "parsed": ParsedQuery 的 dict,
          "next_cursor": str | None,
          "total": int,
          "hint": str,
        }
    """
    try:
        effective_q = query
        parsed_obj: ParsedQuery | None = None
        if auto_parse:
            parsed_obj = await parse_query(query)
            effective_q, from_date, to_date = resolve_auto_parsed_query(
                query,
                parsed_obj,
                from_date=from_date,
                to_date=to_date,
            )

        constraints = (
            extract_structured_constraints(query) if verify_constraints else []
        )

        query_vec, _ = await get_query_embedding(effective_q)
        profile = await get_user_profile(db, user_id)

        conds = [Photo.user_id == user_id, Photo.embedding.is_not(None)]
        if status:
            conds.append(Photo.status == status)

        if from_date:
            conds.append(
                Photo.taken_at
                >= datetime.combine(from_date, time.min, tzinfo=timezone.utc)
            )
        if to_date:
            conds.append(
                Photo.taken_at
                <= datetime.combine(to_date, time.max, tzinfo=timezone.utc)
            )

        if tags:
            subq = (
                select(PhotoTag.photo_id)
                .join(Tag, PhotoTag.tag_id == Tag.id)
                .where(Tag.user_id == user_id, Tag.name.in_(tags))
            )
            conds.append(Photo.id.in_(subq))

        # 结构化分析 JSONB 过滤（OR 语义）
        jsonb_conds = []
        if scene:
            jsonb_conds.append(Photo.ai_analysis["scene"].astext == scene)
        if objects:
            jsonb_conds.append(
                Photo.ai_analysis["objects"].op("?|")(func.array(objects))
            )
        if text_in_image:
            jsonb_conds.append(
                Photo.ai_analysis["text_in_image"].op("?|")(func.array(text_in_image))
            )
        if mood:
            jsonb_conds.append(Photo.ai_analysis["mood"].astext == mood)
        if colors:
            jsonb_conds.append(Photo.ai_analysis["colors"].op("?|")(func.array(colors)))
        if jsonb_conds:
            conds.append(or_(*jsonb_conds))

        fetch_n = max(limit * 5, 30) if constraints else limit * 3
        dist_col = Photo.embedding.cosine_distance(query_vec).label("dist")
        stmt = (
            select(Photo, dist_col)
            .where(and_(*conds))
            .order_by(dist_col)
            .limit(fetch_n)
        )
        result = await db.execute(stmt)
        rows = result.all()

        scored: list[tuple[Photo, float, float, float, float]] = []
        for photo, dist in rows:
            s_sem = semantic_score(float(dist))
            s_rec = recency_score(photo.taken_at)
            s_int = personalized_interaction_score(profile, photo)
            final = combine(s_sem, s_rec, s_int, w_semantic, w_recency, w_interaction)
            scored.append((photo, s_sem, s_rec, s_int, final))

        scored.sort(key=lambda x: x[4], reverse=True)

        scored, constraint_summary = validate_scored_candidates(scored, constraints)

        if cursor:
            parsed_cursor = decode_cursor(cursor)
            if parsed_cursor is not None:
                cur_score, cur_id = parsed_cursor
                scored = [
                    (p, sem, rec, inter, fin)
                    for (p, sem, rec, inter, fin) in scored
                    if fin < cur_score
                    or (abs(fin - cur_score) < 1e-9 and str(p.id) > cur_id)
                ]

        scored, rerank_summary = await rerank_scored_candidates(
            scored,
            query,
            enabled=verify_semantic,
            page_limit=limit,
        )

        page = scored[:limit]
        items = [
            _photo_to_search_item(p, sem, rec, inter, fin)
            for (p, sem, rec, inter, fin) in page
        ]
        next_cursor = None
        if len(page) == limit and len(scored) > limit:
            last_p, _, _, _, last_score = sorted(
                page, key=lambda row: (-row[4], str(row[0].id))
            )[-1]
            next_cursor = encode_cursor(last_score, last_p.id)

        parsed_dict = parsed_obj.model_dump() if parsed_obj else None
        hint = (
            f"找到 {len(items)} 张相关照片"
            if items
            else "未找到匹配照片，建议尝试更宽泛的描述或时间范围"
        )

        return {
            "ok": True,
            "items": items,
            "parsed": parsed_dict,
            "next_cursor": next_cursor,
            "total": len(items),
            "constraint_check": constraint_summary,
            "rerank_check": rerank_summary,
            "hint": hint,
        }

    except Exception as exc:
        logger.exception("search_photos failed | user=%s query=%s", user_id, query)
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "items": [],
            "parsed": None,
            "next_cursor": None,
            "total": 0,
            "hint": f"搜索出现异常：{exc}，请换个说法或稍后再试",
        }


# ------------------------------------------------------------------
# Tool 2: 浏览候选照片（最终兜底 / 时间线浏览）
# ------------------------------------------------------------------
async def browse_candidates(
    *,
    user_id: UUID,
    db: AsyncSession,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Agent 浏览 Tool：按时间倒序列出用户照片，作为最终兜底手段。

    返回：
        {
          "ok": bool,
          "items": [ {...}, ... ],
          "next_cursor": str | None,
          "total": int,
          "hint": str,
        }
    """
    try:
        conds = [Photo.user_id == user_id]
        if from_date:
            conds.append(
                Photo.taken_at
                >= datetime.combine(from_date, time.min, tzinfo=timezone.utc)
            )
        if to_date:
            conds.append(
                Photo.taken_at
                <= datetime.combine(to_date, time.max, tzinfo=timezone.utc)
            )

        stmt = (
            select(Photo)
            .where(and_(*conds))
            .order_by(Photo.taken_at.desc().nullslast())
            .limit(limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_more = len(rows) > limit
        page = rows[:limit]
        items = [_photo_to_search_item(p, 0.0, 0.0, 0.0, 0.0) for p in page]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(0.0, last.id)

        hint = (
            f"共列出 {len(items)} 张照片，请用户从中选择"
            if items
            else "相册中暂时没有照片"
        )

        return {
            "ok": True,
            "items": items,
            "next_cursor": next_cursor,
            "total": len(items),
            "hint": hint,
        }

    except Exception as exc:
        logger.exception("browse_candidates failed | user=%s", user_id)
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "items": [],
            "next_cursor": None,
            "total": 0,
            "hint": f"加载相册失败：{exc}",
        }


# ------------------------------------------------------------------
# Tool 3: 应用 Skill 做 AI 改造
# ------------------------------------------------------------------
async def apply_skill(
    *,
    user_id: UUID,
    db: AsyncSession,
    photo_id: UUID,
    skill_id: UUID | None = None,
    extra_prompt: str | None = None,
    model: str | None = None,
) -> dict:
    """Agent 生图 Tool：校验权限和额度后创建生成任务并入队。

    返回：
        {
          "ok": bool,
          "generation_id": str | None,
          "status": str,
          "hint": str,
        }
    """
    try:
        # 1. 校验源照片
        photo = (
            await db.execute(
                select(Photo).where(
                    and_(Photo.id == photo_id, Photo.user_id == user_id)
                )
            )
        ).scalar_one_or_none()
        if photo is None:
            return {
                "ok": False,
                "generation_id": None,
                "status": "not_found",
                "hint": "未找到这张照片，可能是权限不足或 ID 错误",
            }

        # 2. 校验 Skill
        selected_model = model or "wanx2.1-imageedit"
        skill: Skill | None = None
        if skill_id:
            skill = (
                await db.execute(select(Skill).where(Skill.id == skill_id))
            ).scalar_one_or_none()
            if skill is None:
                return {
                    "ok": False,
                    "generation_id": None,
                    "status": "skill_not_found",
                    "hint": "未找到指定的 Skill",
                }
            if not (skill.is_official or skill.is_public or skill.owner_id == user_id):
                return {
                    "ok": False,
                    "generation_id": None,
                    "status": "skill_forbidden",
                    "hint": "没有权限使用这个 Skill",
                }
            selected_model = skill.model

        # 3. 额度检查
        ok, used, quota = await _check_generate_quota(db, user_id)
        if not ok:
            return {
                "ok": False,
                "generation_id": None,
                "status": "quota_exceeded",
                "hint": (
                    f"今日免费额度已用完（{used}/{quota}），" "明天再来或订阅解锁。"
                ),
            }

        # 4. 落库并入队
        gen = Generation(
            user_id=user_id,
            source_photo_id=photo.id,
            skill_id=skill.id if skill else None,
            extra_prompt=extra_prompt,
            model=selected_model,
            status="pending",
        )
        db.add(gen)
        await db.commit()
        await db.refresh(gen)
        await enqueue_generate_photo(gen.id)

        return {
            "ok": True,
            "generation_id": str(gen.id),
            "status": gen.status,
            "hint": "生成任务已提交，稍后可在生成历史中查看结果",
        }

    except Exception as exc:
        logger.exception(
            "apply_skill failed | user=%s photo=%s skill=%s",
            user_id,
            photo_id,
            skill_id,
        )
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "generation_id": None,
            "status": "error",
            "hint": f"生成任务创建失败：{exc}",
        }


# ------------------------------------------------------------------
# Tool 4: 获取单张照片详情（Agent 必要时可调用）
# ------------------------------------------------------------------
async def get_photo_detail(
    *,
    user_id: UUID,
    db: AsyncSession,
    photo_id: UUID,
) -> dict:
    """获取单张照片的完整结构化信息。"""
    try:
        photo = (
            await db.execute(
                select(Photo).where(
                    and_(Photo.id == photo_id, Photo.user_id == user_id)
                )
            )
        ).scalar_one_or_none()
        if photo is None:
            return {
                "ok": False,
                "data": None,
                "hint": "照片不存在或无权访问",
            }

        return {
            "ok": True,
            "data": _photo_to_search_item(photo, 0.0, 0.0, 0.0, 0.0),
            "hint": "已获取照片详情",
        }
    except Exception as exc:
        logger.exception(
            "get_photo_detail failed | user=%s photo=%s", user_id, photo_id
        )
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "data": None,
            "hint": f"获取照片详情失败：{exc}",
        }


# ------------------------------------------------------------------
# Tool 5: 三级兜底搜索（clue album → timeline → full album）
# ------------------------------------------------------------------
async def fallback_search(
    *,
    user_id: UUID,
    db: AsyncSession,
    query: str,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 30,
    start_level: int = 0,
) -> dict:
    """当普通搜索找不到结果时，按三级策略逐步放宽条件。

    Args:
        start_level: 从第几级开始兜底。
            0=包含普通语义搜索（默认）；
            1=跳过普通搜索，直接从线索相册开始（用于自动兜底，避免重复搜索）。

    返回：
        {
          "ok": bool,
          "items": [...],
          "fallback_level": 0|1|2|3,
          # 0=普通搜索命中；1=放宽状态命中；2=时间线兜底命中；3=全相册兜底命中
          "hint": str,
        }
    """
    # Level 0: 普通语义搜索（status=done）— 可被 start_level 跳过
    if start_level <= 0:
        res = await search_photos(
            user_id=user_id,
            db=db,
            query=query,
            from_date=from_date,
            to_date=to_date,
            status="done",
            limit=limit,
        )
        if res.get("ok") and res.get("items"):
            return {
                "ok": True,
                "items": res["items"],
                "fallback_level": 0,
                "hint": res["hint"],
            }

    # Level 1: clue album — 放宽处理状态，允许 pending/processing/partial_done 也参与
    res = await search_photos(
        user_id=user_id,
        db=db,
        query=query,
        from_date=from_date,
        to_date=to_date,
        status=None,
        limit=limit,
    )
    if res.get("ok") and res.get("items"):
        return {
            "ok": True,
            "items": res["items"],
            "fallback_level": 1,
            "hint": f"【线索相册】{res['hint']}",
        }

    if res.get("ok") and res.get("constraint_check", {}).get("applied"):
        return {
            "ok": True,
            "items": [],
            "fallback_level": 1,
            "constraint_check": res["constraint_check"],
            "hint": "相册中没有找到满足文字、品牌、数值或路线等强约束的照片",
        }

    # Level 2: timeline 兜底 — 按时间范围列出照片
    if from_date or to_date:
        res = await browse_candidates(
            user_id=user_id,
            db=db,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        if res.get("ok") and res.get("items"):
            return {
                "ok": True,
                "items": res["items"],
                "fallback_level": 2,
                "hint": f"【时间线兜底】{res['hint']}",
            }

    # Level 3: 全相册兜底
    res = await browse_candidates(
        user_id=user_id,
        db=db,
        limit=limit,
    )
    return {
        "ok": res.get("ok", False),
        "items": res.get("items", []),
        "fallback_level": 3,
        "hint": f"【全相册兜底】{res['hint']}"
        if res.get("ok")
        else res.get("hint", "无兜底结果"),
    }


# ------------------------------------------------------------------
# Tool 6: 主动推荐 Skill
# ------------------------------------------------------------------
async def recommend_skills_for_agent(
    *,
    user_id: UUID,
    db: AsyncSession,
    photo_ids: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """基于用户画像和上下文照片为用户推荐 Skill。

    返回：
        {
          "ok": bool,
          "items": [ {...}, ... ],
          "hint": str,
        }
    """
    try:
        pids = [UUID(pid) for pid in (photo_ids or []) if pid]
        items = await recommend_skills(
            db=db,
            user_id=user_id,
            photo_ids=pids,
            limit=limit,
        )
        return {
            "ok": True,
            "items": items,
            "hint": f"为你推荐 {len(items)} 个 Skill" if items else "暂无匹配 Skill",
        }
    except Exception as exc:
        logger.exception("recommend_skills_for_agent failed | user=%s", user_id)
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "items": [],
            "hint": f"推荐失败：{exc}",
        }
