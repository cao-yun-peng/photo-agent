"""Search 路由：pgvector 语义 + 多维过滤 + 混合排序 + 游标分页."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.photo import Photo
from app.models.user import User
from app.schemas.photo import (
    AlbumFallbackQuery,
    SearchClick,
    SearchQuery,
    SearchResult,
)
from app.services.events import log_event

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
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest, SearchError

    try:
        result = await SearchService(db, current_user.id).search(
            SearchRequest(**payload.model_dump(), include_index_coverage=True)
        )
        return SearchResult.model_validate(result)
    except SearchError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={
                "code": exc.code,
                "message": "搜索状态已变化，请重新搜索或稍后重试",
            },
        ) from exc


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
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest, SearchError

    try:
        result = await SearchService(db, current_user.id).search(
            SearchRequest(
                q=payload.q or " ",
                retrieval_mode="album",
                limit=payload.limit,
                cursor=payload.cursor,
                w_semantic=payload.w_semantic,
                w_recency=payload.w_recency,
                w_interaction=payload.w_interaction,
                status=None,
                result_mode="select",
                verify_constraints=False,
                verify_semantic=False,
                include_index_coverage=True,
            )
        )
        return SearchResult.model_validate(result)
    except SearchError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": "相册分页不可用，请重新浏览"},
        ) from exc
