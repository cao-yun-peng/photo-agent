"""Album and timeline browsing through SQL keysets, bounded rows per page."""

from datetime import datetime
from types import SimpleNamespace
from uuid import UUID
from sqlalchemy import select, func, or_, and_, true
from app.models.photo import Photo
from app.services.search_sql import SEARCH_COLUMNS, score_expressions
from app.services.search_store import encode_cursor
from app.services.search_contracts import SearchError


async def album_page(service, request, plan, position, context=None):
    # This is a live album window, not a materialized semantic candidate snapshot.
    if position != 0 and not isinstance(position, dict):
        raise SearchError("invalid_cursor_offset", 400)
    policy = plan.request()
    conds, *_ = service.repository.filters(
        policy, plan.timezone, excluded=request.exclude_photo_ids
    )
    # Freeze insertion boundary and omit edits; never duplicate an item moved by an edit.
    cutoff = datetime.fromisoformat(plan.scoring_time)
    conds += [Photo.created_at <= cutoff, Photo.updated_at <= cutoff]
    limit = min(request.limit, 100)
    ranked = policy.retrieval_mode == "album"
    context = context or {}
    profile = SimpleNamespace(**context["profile"]) if context.get("profile") else None
    scores = score_expressions(policy, plan, context.get("vector"), profile)
    if position and (
        (ranked and "score" not in position)
        or (not ranked and "taken_at" not in position)
    ):
        raise SearchError("invalid_cursor_offset", 400)
    if position and ranked:
        edge = or_(
            scores[-1] < position["score"],
            and_(scores[-1] == position["score"], Photo.id > UUID(position["id"])),
        )
    elif position:
        pid = UUID(position["id"])
        taken = (
            datetime.fromisoformat(position["taken_at"])
            if position["taken_at"]
            else None
        )
        edge = (
            and_(Photo.taken_at.is_(None), Photo.id > pid)
            if taken is None
            else or_(
                Photo.taken_at < taken,
                Photo.taken_at.is_(None),
                and_(Photo.taken_at == taken, Photo.id > pid),
            )
        )
    else:
        edge = True
    # A single statement gives the count and page the same MVCC view.
    total = select(func.count()).select_from(Photo).where(*conds).scalar_subquery()
    page = (
        select(*SEARCH_COLUMNS, *scores)
        .where(*conds, edge)
        .order_by(
            *(
                (scores[-1].desc(), Photo.id.asc())
                if ranked
                else (Photo.taken_at.desc().nullslast(), Photo.id.asc())
            )
        )
        .limit(limit + 1)
        .subquery()
    )
    anchor = select(total.label("total")).subquery()
    rows = (
        (
            await service.db.execute(
                select(anchor.c.total, *page.c)
                .select_from(anchor.outerjoin(page, true()))
                .order_by(
                    *(
                        (page.c.final.desc(), page.c.id.asc())
                        if ranked
                        else (page.c.taken_at.desc().nullslast(), page.c.id.asc())
                    )
                )
            )
        )
        .mappings()
        .all()
    )
    count = rows[0]["total"]
    values = [row for row in rows if row["id"] is not None]
    has_more = len(values) > limit
    values = values[:limit]
    from app.services.search_engine import serialize_photo

    items = [
        serialize_photo(
            SimpleNamespace(**{c.key: row[c.key] for c in SEARCH_COLUMNS}),
            (row["sem"], row["rec"], row["inter"], row["final"]),
        )
        for row in values
    ]
    next_cursor = None
    if has_more:
        last = values[-1]
        next_cursor = encode_cursor(
            service.user_id,
            plan.id,
            (
                {"id": str(last["id"]), "score": last["final"]}
                if ranked
                else {
                    "id": str(last["id"]),
                    "taken_at": last["taken_at"].isoformat()
                    if last["taken_at"]
                    else None,
                }
            ),
            plan.expires_at,
        )
    return {
        "items": items,
        "total": len(items),
        "total_matches": count,
        "result_mode": "select",
        "next_cursor": next_cursor,
        "truncated": has_more,
        "result_set_complete": False,
        "completeness_reason": "live_album_window",
        "stop_reason": "page_full" if has_more else "candidates_exhausted",
        "search_exhausted": False,
        "search_id": str(plan.id),
        "_search_plan_id": str(plan.id),
        "selection_owner": "user",
        "unverified_count": len(items),
        "browse_scope": "all",
        "hint": "这些是相册浏览照片，未经搜索相关性核验，并非确认匹配结果。",
    }
