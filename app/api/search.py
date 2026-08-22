"""Search 路由：pgvector 语义 + 多维过滤 + 混合排序 + 游标分页."""

from datetime import datetime, time, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.photo import Photo
from app.models.tag import PhotoTag, Tag
from app.models.user import User
from app.schemas.photo import (
    AlbumFallbackQuery,
    SearchClick,
    SearchConstraintCheck,
    SearchQuery,
    SearchRerankCheck,
    SearchIndexCoverage,
    SearchResult,
    SearchResultItem,
)
from app.services.events import log_event
from app.services.oss import sign_get_url
from app.services.query_parser import parse_query, resolve_auto_parsed_query
from app.services.search_constraints import (
    extract_structured_constraints,
    validate_scored_candidates,
)
from app.services.search_reranker import rerank_scored_candidates
from app.services.search_index import get_index_coverage
from app.services.search import (
    apply_semantic_threshold,
    build_search_coverage_hint,
    combine,
    complete_scope_is_reliable,
    decode_cursor,
    get_query_embedding,
    get_user_profile,
    infer_complete_result_filters,
    personalized_interaction_score,
    recency_score,
    resolve_semantic_threshold,
    resolve_search_result_limits,
    semantic_score,
    smart_album_fallback,
)

router = APIRouter()


async def _get_index_coverage(db: AsyncSession, user_id) -> SearchIndexCoverage:
    """兼容现有路由测试，同时复用公共索引覆盖率实现。"""
    coverage = await get_index_coverage(db, user_id)
    coverage.pop("complete", None)
    return SearchIndexCoverage(**coverage)


@router.post(
    "",
    response_model=SearchResult,
    summary="按自然语言语义搜索（支持时间/标签/游标/自动解析）",
)
async def semantic_search(
    payload: SearchQuery,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SearchResult:
    output_limit, verification_limit = resolve_search_result_limits(
        payload.result_mode,
        payload.limit,
        complete_result_set=payload.complete_result_set,
    )
    index_coverage = await _get_index_coverage(db, current_user.id)
    # ---------- 1. 可选：自动解析自然语言条件 ----------
    parsed = None
    if payload.auto_parse:
        parsed = await parse_query(payload.q)
        effective_q, payload.from_date, payload.to_date = resolve_auto_parsed_query(
            payload.q,
            parsed,
            from_date=payload.from_date,
            to_date=payload.to_date,
        )
    else:
        effective_q = payload.q

    constraints = (
        extract_structured_constraints(payload.q) if payload.verify_constraints else []
    )

    # ---------- 2. 查询向量 (带缓存) ----------
    query_vec, cache_hit = await get_query_embedding(effective_q)

    # ---------- 2.5 读取用户画像（用于个性化排序） ----------
    profile = await get_user_profile(db, current_user.id)

    # ---------- 3. 构造过滤条件 ----------
    conds = [Photo.user_id == current_user.id, Photo.embedding.is_not(None)]
    if payload.status:
        conds.append(Photo.status == payload.status)

    if payload.from_date:
        conds.append(
            Photo.taken_at
            >= datetime.combine(payload.from_date, time.min, tzinfo=timezone.utc)
        )
    if payload.to_date:
        conds.append(
            Photo.taken_at
            <= datetime.combine(payload.to_date, time.max, tzinfo=timezone.utc)
        )

    # tags：命中任一即可（OR 语义）
    if payload.tags:
        subq = (
            select(PhotoTag.photo_id)
            .join(Tag, PhotoTag.tag_id == Tag.id)
            .where(Tag.user_id == current_user.id, Tag.name.in_(payload.tags))
        )
        conds.append(Photo.id.in_(subq))

    # 结构化分析 JSONB 过滤（命中任一条件即可，OR 语义）
    jsonb_conds = []
    if payload.scene:
        jsonb_conds.append(Photo.ai_analysis["scene"].astext == payload.scene)
    if payload.objects:
        jsonb_conds.append(
            Photo.ai_analysis["objects"].op("?|")(func.array(payload.objects))
        )
    if payload.text_in_image:
        jsonb_conds.append(
            Photo.ai_analysis["text_in_image"].op("?|")(
                func.array(payload.text_in_image)
            )
        )
    if payload.mood:
        jsonb_conds.append(Photo.ai_analysis["mood"].astext == payload.mood)
    if payload.colors:
        jsonb_conds.append(
            Photo.ai_analysis["colors"].op("?|")(func.array(payload.colors))
        )
    if jsonb_conds:
        conds.append(or_(*jsonb_conds))

    inferred_filters = infer_complete_result_filters(payload.q)
    if payload.complete_result_set and not complete_scope_is_reliable(
        inferred_filters,
        tags=payload.tags,
        scene=payload.scene,
        objects=payload.objects,
        text_in_image=payload.text_in_image,
        mood=payload.mood,
        colors=payload.colors,
        photo_types=payload.photo_types,
        is_selfie=payload.is_selfie,
        people_count_min=payload.people_count_min,
        people_count_max=payload.people_count_max,
    ):
        return SearchResult(
            items=[],
            total=0,
            total_matches=0,
            result_mode=payload.result_mode,
            result_set_complete=False,
            completeness_reason="semantic_scope_unverified",
            index_coverage=index_coverage,
            coverage_hint=(
                "开放语义类别尚不能保证完整，已停止把全相册作为匹配结果返回"
            ),
        )
    effective_photo_types = list(payload.photo_types or inferred_filters["photo_types"])
    effective_is_selfie = (
        payload.is_selfie
        if payload.is_selfie is not None
        else inferred_filters["is_selfie"]
    )
    effective_people_min = (
        payload.people_count_min
        if payload.people_count_min is not None
        else inferred_filters["people_count_min"]
    )
    effective_people_max = (
        payload.people_count_max
        if payload.people_count_max is not None
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
        payload.min_semantic_score,
        structured_collection=structured_collection,
    )

    # ---------- 4. 向量召回（强约束查询扩大候选池后再做证据校验） ----------
    if verification_limit is None:
        fetch_n = None
    else:
        fetch_n = (
            max(verification_limit * 5, 30) if constraints else verification_limit * 3
        )
    dist_col = Photo.embedding.cosine_distance(query_vec).label("dist")
    stmt = select(Photo, dist_col).where(and_(*conds)).order_by(dist_col)
    if fetch_n is not None:
        stmt = stmt.limit(fetch_n)
    result = await db.execute(stmt)
    rows = result.all()  # [(Photo, dist), ...]

    # ---------- 5. 混合评分 + 排序 ----------
    scored: list[tuple[Photo, float, float, float, float]] = []
    for photo, dist in rows:
        s_sem = semantic_score(float(dist))
        s_rec = recency_score(photo.taken_at)
        s_int = personalized_interaction_score(profile, photo)
        final = combine(
            s_sem,
            s_rec,
            s_int,
            payload.w_semantic,
            payload.w_recency,
            payload.w_interaction,
        )
        scored.append((photo, s_sem, s_rec, s_int, final))

    scored.sort(key=lambda x: x[4], reverse=True)

    scored, constraint_summary = validate_scored_candidates(scored, constraints)
    scored, threshold_filtered_count = apply_semantic_threshold(
        scored, similarity_threshold
    )

    # ---------- 6. 游标分页 ----------
    if payload.cursor:
        parsed_cursor = decode_cursor(payload.cursor)
        if parsed_cursor is not None:
            cur_score, cur_id = parsed_cursor
            # 保留 final < cur_score，或 final==cur_score 且 id > cur_id 的
            scored = [
                (p, sem, rec, inter, fin)
                for (p, sem, rec, inter, fin) in scored
                if fin < cur_score
                or (abs(fin - cur_score) < 1e-9 and str(p.id) > cur_id)
            ]

    # 用户自选模式不能被判同模型删减为少量“最佳”结果；保留向量相似度
    # 与硬条件过滤后的完整 Top-N，最终选择权属于用户。
    scored, rerank_summary = await rerank_scored_candidates(
        scored,
        payload.q,
        enabled=payload.verify_semantic and payload.result_mode != "select",
        page_limit=verification_limit or len(scored),
    )

    page = scored if output_limit is None else scored[:output_limit]

    # ---------- 7. 拼接返回 ----------
    items = [
        SearchResultItem(
            id=p.id,
            thumb_url=sign_get_url(p.thumb_key or p.oss_key),
            taken_at=p.taken_at,
            ai_description=p.ai_description,
            status=p.status,
            score_semantic=round(sem, 4),
            score_recency=round(rec, 4),
            score_interaction=round(inter, 4),
            score_final=round(fin, 4),
        )
        for (p, sem, rec, inter, fin) in page
    ]

    embedding_coverage_complete = bool(
        index_coverage.unavailable_photos == 0
        and index_coverage.retrying_photos == 0
        and index_coverage.indexed_photos >= index_coverage.total_photos
    )
    coverage_complete = bool(
        embedding_coverage_complete
        and (index_coverage.semantic_complete if structured_collection else True)
    )
    scope_reliable = complete_scope_is_reliable(
        inferred_filters,
        tags=payload.tags,
        scene=payload.scene,
        objects=payload.objects,
        text_in_image=payload.text_in_image,
        mood=payload.mood,
        colors=payload.colors,
        photo_types=payload.photo_types,
        is_selfie=payload.is_selfie,
        people_count_min=payload.people_count_min,
        people_count_max=payload.people_count_max,
    )
    result_set_complete = bool(
        payload.result_mode == "select"
        and payload.complete_result_set
        and coverage_complete
        and scope_reliable
    )
    if not payload.complete_result_set:
        completeness_reason = "not_requested"
    elif not coverage_complete:
        completeness_reason = "index_incomplete"
    elif not scope_reliable:
        completeness_reason = "semantic_scope_unverified"
    else:
        completeness_reason = None
    coverage_payload = index_coverage.model_dump()
    coverage_payload["complete"] = embedding_coverage_complete
    coverage_hint = build_search_coverage_hint(
        coverage_payload,
        requires_facets=structured_collection,
        threshold=similarity_threshold,
        threshold_filtered_count=threshold_filtered_count,
    )

    return SearchResult(
        items=items,
        total=len(page),
        total_matches=len(scored) if payload.complete_result_set else len(page),
        result_mode=payload.result_mode,
        result_set_complete=result_set_complete,
        completeness_reason=completeness_reason,
        truncated=len(page) < len(scored),
        next_cursor=None,
        parsed=parsed,
        cache_hit=cache_hit,
        constraint_check=(
            SearchConstraintCheck(**constraint_summary)
            if constraint_summary is not None
            else None
        ),
        rerank_check=(
            SearchRerankCheck(**rerank_summary) if rerank_summary is not None else None
        ),
        index_coverage=index_coverage,
        similarity_threshold=similarity_threshold,
        threshold_filtered_count=threshold_filtered_count,
        threshold_bypassed_reason=threshold_bypassed_reason,
        coverage_hint=coverage_hint,
        semantic_facets_required=structured_collection,
    )


@router.post(
    "/click",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="上报用户点击了某条搜索结果",
)
async def report_search_click(
    payload: SearchClick,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """前端在用户点击搜索结果卡片时调用，写入 search_click 事件。"""
    photo = (
        await db.execute(
            select(Photo).where(
                and_(Photo.id == payload.photo_id, Photo.user_id == current_user.id)
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    await log_event(
        user_id=current_user.id,
        event_type="search_click",
        payload={
            "photo_id": str(payload.photo_id),
            "query": payload.query,
            "rank": payload.rank,
        },
    )


@router.post(
    "/album-fallback",
    response_model=SearchResult,
    summary="智能全量相册兜底（语义+新鲜度+个性化排序）",
)
async def album_fallback(
    payload: AlbumFallbackQuery,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SearchResult:
    """当普通搜索无结果时，返回用户全量相册的智能排序结果。"""
    index_coverage = await _get_index_coverage(db, current_user.id)
    page, next_cursor = await smart_album_fallback(
        db=db,
        user_id=current_user.id,
        query=payload.q,
        limit=payload.limit,
        cursor=payload.cursor,
        w_semantic=payload.w_semantic,
        w_recency=payload.w_recency,
        w_interaction=payload.w_interaction,
    )

    items = [
        SearchResultItem(
            id=p.id,
            thumb_url=sign_get_url(p.thumb_key or p.oss_key),
            taken_at=p.taken_at,
            ai_description=p.ai_description,
            status=p.status,
            score_semantic=round(sem, 4),
            score_recency=round(rec, 4),
            score_interaction=round(inter, 4),
            score_final=round(fin, 4),
        )
        for (p, sem, rec, inter, fin) in page
    ]

    return SearchResult(
        items=items,
        total=len(items),
        next_cursor=next_cursor,
        parsed=None,
        cache_hit=False,
        index_coverage=index_coverage,
    )
