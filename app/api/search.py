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
from app.services.search import (
    combine,
    decode_cursor,
    encode_cursor,
    get_query_embedding,
    get_user_profile,
    personalized_interaction_score,
    recency_score,
    semantic_score,
    smart_album_fallback,
)

router = APIRouter()


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

    # ---------- 4. 向量召回（强约束查询扩大候选池后再做证据校验） ----------
    fetch_n = max(payload.limit * 5, 30) if constraints else payload.limit * 3
    dist_col = Photo.embedding.cosine_distance(query_vec).label("dist")
    stmt = select(Photo, dist_col).where(and_(*conds)).order_by(dist_col).limit(fetch_n)
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

    scored, rerank_summary = await rerank_scored_candidates(
        scored,
        payload.q,
        enabled=payload.verify_semantic,
        page_limit=payload.limit,
    )

    page = scored[: payload.limit]

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

    next_cursor = None
    if len(page) == payload.limit and len(scored) > payload.limit:
        last_p, _, _, _, last_score = sorted(
            page, key=lambda row: (-row[4], str(row[0].id))
        )[-1]
        next_cursor = encode_cursor(last_score, last_p.id)

    return SearchResult(
        items=items,
        total=len(page),
        next_cursor=next_cursor,
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
    )
