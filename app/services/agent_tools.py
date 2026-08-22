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

from app.core.telemetry import set_current_span_attributes, traced_async
from app.models.photo import Photo
from app.models.tag import PhotoTag, Tag
from app.schemas.photo import ParsedQuery
from app.services.oss import sign_get_url
from app.services.generation_service import (
    GenerationDomainError,
    confirm_generation,
    generation_confirmation_payload,
    prepare_generation,
)
from app.services.query_parser import parse_query, resolve_auto_parsed_query
from app.services.search_constraints import (
    extract_structured_constraints,
    validate_scored_candidates,
)
from app.services.search_index import get_index_coverage
from app.services.search_reranker import (
    rerank_scored_candidates,
    verify_scored_candidate_pool,
)
from app.services.recommend import recommend_skills
from app.services.search import (
    apply_semantic_threshold,
    build_search_coverage_hint,
    combine,
    complete_scope_is_reliable,
    decode_cursor,
    encode_cursor,
    get_query_embedding,
    get_user_profile,
    infer_complete_result_filters,
    personalized_interaction_score,
    recency_score,
    resolve_semantic_threshold,
    resolve_search_result_limits,
    semantic_score,
)

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


# ------------------------------------------------------------------
# Tool 1: 搜索照片
# ------------------------------------------------------------------
@traced_async(
    "search retrieve",
    attributes={"gen_ai.operation.name": "retrieval"},
)
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
    photo_types: list[str] | None = None,
    is_selfie: bool | None = None,
    people_count_min: int | None = None,
    people_count_max: int | None = None,
    min_semantic_score: float | None = None,
    status: str | None = "done",
    limit: int = 10,
    cursor: str | None = None,
    exclude_photo_ids: list[str] | None = None,
    auto_parse: bool = True,
    verify_constraints: bool = True,
    verify_semantic: bool = True,
    result_mode: str = "browse",
    complete_result_set: bool = False,
    verified_only: bool = False,
    candidate_pool_size: int = 12,
    force_visual_verify: bool = False,
    include_index_coverage: bool = False,
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
        output_limit, verification_limit = resolve_search_result_limits(
            result_mode,
            limit,
            complete_result_set=complete_result_set,
        )
        candidate_pool_size = max(1, min(int(candidate_pool_size), 30))
        if verified_only:
            output_limit = max(1, min(int(limit), candidate_pool_size))
            verification_limit = candidate_pool_size
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
        excluded_ids: list[UUID] = []
        for raw_id in exclude_photo_ids or []:
            try:
                excluded_ids.append(UUID(str(raw_id)))
            except (TypeError, ValueError, AttributeError):
                continue
        if excluded_ids:
            conds.append(Photo.id.notin_(excluded_ids))

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

        inferred_filters = infer_complete_result_filters(query)
        if complete_result_set and not complete_scope_is_reliable(
            inferred_filters,
            tags=tags,
            scene=scene,
            objects=objects,
            text_in_image=text_in_image,
            mood=mood,
            colors=colors,
            photo_types=photo_types,
            is_selfie=is_selfie,
            people_count_min=people_count_min,
            people_count_max=people_count_max,
        ):
            return {
                "ok": True,
                "items": [],
                "total": 0,
                "total_matches": 0,
                "result_mode": result_mode,
                "complete_result_set": True,
                "result_set_complete": False,
                "completeness_reason": "semantic_scope_unverified",
                "retrieval_strategy": "exhaustive_semantic_required",
                "hint": (
                    "这个‘全部’请求属于开放语义类别，当前不能用一次向量搜索"
                    "保证完整；已停止返回全相册，需进入完整语义检索。"
                ),
            }
        effective_photo_types = list(photo_types or inferred_filters["photo_types"])
        effective_is_selfie = (
            is_selfie if is_selfie is not None else inferred_filters["is_selfie"]
        )
        effective_people_min = (
            people_count_min
            if people_count_min is not None
            else inferred_filters["people_count_min"]
        )
        effective_people_max = (
            people_count_max
            if people_count_max is not None
            else inferred_filters["people_count_max"]
        )
        if effective_photo_types:
            conds.append(Photo.photo_type.in_(effective_photo_types))
        if effective_is_selfie is not None:
            conds.append(Photo.is_selfie == effective_is_selfie)
        if effective_people_min is not None:
            conds.append(Photo.people_count >= effective_people_min)
        if effective_people_max is not None:
            conds.append(Photo.people_count <= effective_people_max)
        structured_collection = bool(
            effective_photo_types
            or effective_is_selfie is not None
            or effective_people_min is not None
            or effective_people_max is not None
        )
        similarity_threshold, threshold_bypassed_reason = resolve_semantic_threshold(
            min_semantic_score,
            structured_collection=structured_collection,
        )

        if verification_limit is None:
            fetch_n = None
        else:
            fetch_n = (
                max(verification_limit * 5, 30)
                if constraints
                else verification_limit * 3
            )
        dist_col = Photo.embedding.cosine_distance(query_vec).label("dist")
        stmt = select(Photo, dist_col).where(and_(*conds)).order_by(dist_col)
        if fetch_n is not None:
            stmt = stmt.limit(fetch_n)
        result = await db.execute(stmt)
        rows = result.all()
        set_current_span_attributes(
            {
                "search.fetch_count": len(rows),
                "search.fetch_limit": fetch_n if fetch_n is not None else -1,
                "search.complete_result_set": complete_result_set,
                "search.constraint_count": len(constraints),
                "search.verified_only": verified_only,
            }
        )

        scored: list[tuple[Photo, float, float, float, float]] = []
        for photo, dist in rows:
            s_sem = semantic_score(float(dist))
            s_rec = recency_score(photo.taken_at)
            s_int = personalized_interaction_score(profile, photo)
            final = combine(s_sem, s_rec, s_int, w_semantic, w_recency, w_interaction)
            scored.append((photo, s_sem, s_rec, s_int, final))

        scored.sort(key=lambda x: x[4], reverse=True)

        scored, constraint_summary = validate_scored_candidates(scored, constraints)
        scored, threshold_filtered_count = apply_semantic_threshold(
            scored, similarity_threshold
        )

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

        if verified_only:
            scored, rerank_summary = await verify_scored_candidate_pool(
                scored,
                query,
                enabled=verify_semantic,
                max_candidates=verification_limit,
                max_results=candidate_pool_size,
                force_visual_on_zero_match=force_visual_verify,
            )
        else:
            scored, rerank_summary = await rerank_scored_candidates(
                scored,
                query,
                # select 是“召回给用户自己选”，不可由判同模型删减候选。
                enabled=verify_semantic and result_mode != "select",
                page_limit=verification_limit or len(scored),
            )

        page = scored if output_limit is None else scored[:output_limit]
        items = [
            _photo_to_search_item(p, sem, rec, inter, fin)
            for (p, sem, rec, inter, fin) in page
        ]
        parsed_dict = parsed_obj.model_dump() if parsed_obj else None
        if not items:
            hint = "未找到匹配照片，建议尝试更宽泛的描述或时间范围"
        elif result_mode == "best":
            hint = "已从 Top-5 候选中选出最佳照片"
        elif result_mode == "select":
            hint = f"找到 {len(items)} 张相似照片，请由你选择最满意的一张"
        else:
            hint = f"找到 {len(items)} 张相关照片供你选择"

        index_coverage = (
            await get_index_coverage(db, user_id)
            if include_index_coverage or structured_collection or complete_result_set
            else None
        )
        coverage_complete = not index_coverage or bool(
            index_coverage.get("complete", True)
            and (
                index_coverage.get("semantic_complete", True)
                if structured_collection
                else True
            )
        )
        total_matches = len(scored) if complete_result_set else len(page)
        scope_reliable = complete_scope_is_reliable(
            inferred_filters,
            tags=tags,
            scene=scene,
            objects=objects,
            text_in_image=text_in_image,
            mood=mood,
            colors=colors,
            photo_types=photo_types,
            is_selfie=is_selfie,
            people_count_min=people_count_min,
            people_count_max=people_count_max,
        )
        result_set_complete = bool(
            result_mode == "select"
            and complete_result_set
            and coverage_complete
            and scope_reliable
        )
        if not complete_result_set:
            completeness_reason = "not_requested"
        elif not coverage_complete:
            completeness_reason = "index_incomplete"
        elif not scope_reliable:
            completeness_reason = "semantic_scope_unverified"
        else:
            completeness_reason = None
        truncated = len(page) < len(scored)
        coverage_hint = build_search_coverage_hint(
            index_coverage,
            requires_facets=structured_collection,
            threshold=similarity_threshold,
            threshold_filtered_count=threshold_filtered_count,
        )
        if coverage_hint:
            hint = f"{hint}。{coverage_hint}"
        remaining_items = (
            [
                _photo_to_search_item(p, sem, rec, inter, fin)
                for (p, sem, rec, inter, fin) in scored[output_limit:]
            ]
            if verified_only
            else []
        )
        rerank_trace = rerank_summary or {}
        set_current_span_attributes(
            {
                "search.constraint_pass_count": len(scored),
                "search.result_count": len(items),
                "search.total_matches": total_matches,
                "search.result_set_complete": result_set_complete,
                "search.candidate_pool_count": len(remaining_items),
                "search.similarity_threshold": similarity_threshold or 0.0,
                "search.threshold_filtered_count": threshold_filtered_count,
                "search.semantic_facets_required": structured_collection,
                "search.rerank_applied": bool(rerank_trace.get("applied")),
                "search.rerank_degraded": bool(rerank_trace.get("degraded")),
                "search.rerank_match_count": int(
                    rerank_trace.get("match_count", 0) or 0
                ),
                "search.visual_applied": bool(
                    rerank_trace.get("visual_verification_applied")
                ),
                "search.visual_match_count": int(
                    rerank_trace.get("visual_match_count", 0) or 0
                ),
            }
        )
        return {
            "ok": True,
            "items": items,
            "_candidate_pool_items": remaining_items,
            "parsed": parsed_dict,
            "next_cursor": None,
            "total": len(items),
            "total_matches": total_matches,
            "result_mode": result_mode,
            "complete_result_set": complete_result_set,
            "result_set_complete": result_set_complete,
            "completeness_reason": completeness_reason,
            "truncated": truncated,
            "inferred_filters": inferred_filters,
            "selection_owner": "system" if result_mode == "best" else "user",
            "constraint_check": constraint_summary,
            "rerank_check": rerank_summary,
            "index_coverage": index_coverage,
            "similarity_threshold": similarity_threshold,
            "threshold_filtered_count": threshold_filtered_count,
            "threshold_bypassed_reason": threshold_bypassed_reason,
            "coverage_hint": coverage_hint,
            "semantic_facets_required": structured_collection,
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
    exclude_photo_ids: list[str] | None = None,
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
        excluded_ids: list[UUID] = []
        for raw_id in exclude_photo_ids or []:
            try:
                excluded_ids.append(UUID(str(raw_id)))
            except (TypeError, ValueError, AttributeError):
                continue
        if excluded_ids:
            conds.append(Photo.id.notin_(excluded_ids))
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
    idempotency_key: str | None = None,
    require_confirmation: bool = True,
) -> dict:
    """准备生成任务；灰度组确认后才会预占额度并入队。

    返回：
        {
          "ok": bool,
          "generation_id": str | None,
          "status": str,
          "hint": str,
        }
    """
    try:
        gen = await prepare_generation(
            db=db,
            user_id=user_id,
            photo_id=photo_id,
            skill_id=skill_id,
            extra_prompt=extra_prompt,
            model=model,
            idempotency_key=idempotency_key,
        )
        if not require_confirmation and gen.status in {
            "awaiting_confirmation",
            "queue_failed",
        }:
            gen = await confirm_generation(
                db=db,
                user_id=user_id,
                generation_id=gen.id,
                confirmation_token=gen.confirmation_token,
            )

        confirmation_required = gen.status == "awaiting_confirmation"

        return {
            "ok": True,
            "generation_id": str(gen.id),
            "status": gen.status,
            "confirmation_required": confirmation_required,
            "confirmation": (
                generation_confirmation_payload(gen) if confirmation_required else None
            ),
            "estimated_cost_yuan": float(gen.estimated_cost_yuan or 0),
            "hint": (
                "请用户确认本次照片、效果和预计费用后再开始生成"
                if confirmation_required
                else "生成任务已提交，稍后可在生成历史中查看结果"
            ),
        }

    except GenerationDomainError as exc:
        return {
            "ok": False,
            "error_type": exc.code,
            "generation_id": None,
            "status": "error",
            "hint": str(exc),
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
    exclude_photo_ids: list[str] | None = None,
    allow_unfiltered_browse: bool = True,
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
            exclude_photo_ids=exclude_photo_ids,
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
        exclude_photo_ids=exclude_photo_ids,
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
            exclude_photo_ids=exclude_photo_ids,
        )
        if res.get("ok") and res.get("items"):
            return {
                "ok": True,
                "items": res["items"],
                "fallback_level": 2,
                "hint": f"【时间线兜底】{res['hint']}",
            }

    # 续搜场景必须保留原语义锚点；没有更多匹配时不能混入全相册无关照片。
    if not allow_unfiltered_browse:
        return {
            "ok": True,
            "items": [],
            "fallback_level": 1,
            "hint": "没有更多符合当前搜索条件的照片",
            "search_exhausted": True,
        }

    # Level 3: 全相册兜底
    res = await browse_candidates(
        user_id=user_id,
        db=db,
        limit=limit,
        exclude_photo_ids=exclude_photo_ids,
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
