"""The sole SQL filter and recall implementation for all search adapters."""

import hashlib
import json
from types import SimpleNamespace
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import load_only
from app.services.search_sql import SEARCH_COLUMNS, score_expressions
from sqlalchemy.dialects.postgresql import array
from app.config import settings
from app.models.photo import Photo
from app.models.tag import PhotoTag, Tag
from app.services.search_time import date_bounds
from app.services.search import (
    infer_complete_result_filters,
    complete_scope_is_reliable,
)


def photo_version(photo):
    value = [
        str(photo.id),
        photo.hash,
        str(photo.updated_at),
        photo.ai_analysis,
        photo.ai_description,
        photo.oss_key,
    ]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


class SearchRepository:
    def __init__(self, db, user_id):
        self.db, self.user_id = db, user_id
        self.approximate = False

    def filters(self, request, timezone, *, excluded=()):
        conds = [Photo.user_id == self.user_id]
        if request.retrieval_mode == "semantic":
            conds.append(Photo.embedding.is_not(None))
        if request.status == "done":
            conds.append(Photo.status.in_(("done", "partial_done")))
        elif request.status:
            conds.append(Photo.status == request.status)
        excluded = set(request.exclude_photo_ids) | set(excluded)
        if excluded:
            conds.append(Photo.id.notin_(excluded))
        start, end = date_bounds(request.from_date, request.to_date, timezone)
        if start:
            conds.append(Photo.taken_at >= start)
        if end:
            conds.append(Photo.taken_at < end)
        if request.tags:
            tags = (
                select(PhotoTag.photo_id)
                .join(Tag, PhotoTag.tag_id == Tag.id)
                .where(Tag.user_id == self.user_id, Tag.name.in_(request.tags))
            )
            conds.append(Photo.id.in_(tags))
        json_filters = []
        for name in ("scene", "mood"):
            if getattr(request, name):
                json_filters.append(
                    Photo.ai_analysis[name].astext == getattr(request, name)
                )
        for name in ("objects", "text_in_image", "colors"):
            if getattr(request, name):
                json_filters.append(
                    Photo.ai_analysis[name].op("?|")(array(getattr(request, name)))
                )
        if json_filters:
            conds.append(or_(*json_filters))
        inferred = (
            infer_complete_result_filters(request.q)
            if request.retrieval_mode == "semantic"
            else {
                "photo_types": [],
                "is_selfie": None,
                "people_count_min": None,
                "people_count_max": None,
            }
        )
        kinds = request.photo_types or inferred["photo_types"]
        selfie = (
            request.is_selfie
            if request.is_selfie is not None
            else inferred["is_selfie"]
        )
        minimum = (
            request.people_count_min
            if request.people_count_min is not None
            else inferred["people_count_min"]
        )
        maximum = (
            request.people_count_max
            if request.people_count_max is not None
            else inferred["people_count_max"]
        )
        if kinds:
            conds.append(Photo.photo_type.in_(kinds))
        if selfie is not None:
            conds.append(Photo.is_selfie == selfie)
        if minimum is not None:
            conds.append(Photo.people_count >= minimum)
        if maximum is not None:
            conds.append(Photo.people_count <= maximum)
        structured = bool(
            kinds or selfie is not None or minimum is not None or maximum is not None
        )
        reliable = complete_scope_is_reliable(
            inferred,
            **request.model_dump(
                include={
                    "tags",
                    "scene",
                    "objects",
                    "text_in_image",
                    "mood",
                    "colors",
                    "photo_types",
                    "is_selfie",
                    "people_count_min",
                    "people_count_max",
                }
            ),
        )
        return conds, inferred, structured, reliable

    def recall_statement(self, request, plan, vector, profile):
        conds, inferred, structured, reliable = self.filters(request, plan.timezone)
        cap = settings.search_snapshot_max_candidates
        scores = score_expressions(request, plan, vector, profile)
        base = select(*SEARCH_COLUMNS, *scores).where(and_(*conds))
        if request.retrieval_mode == "timeline":
            stmt = base.order_by(
                Photo.taken_at.desc().nullslast(), Photo.id.asc()
            ).limit(cap + 1)
        elif request.retrieval_mode == "album":
            # Global hybrid top-k, not a semantic shortlist re-ranked in Python.
            stmt = base.order_by(scores[-1].desc(), Photo.id.asc()).limit(cap + 1)
        elif vector is None:
            stmt = base.order_by(
                Photo.taken_at.desc().nullslast(), Photo.id.asc()
            ).limit(cap + 1)
        else:
            distance = Photo.embedding.cosine_distance(vector)
            # Force exact tie-aware selection by default. ANN has its own explicit path.
            candidates = (
                select(Photo.id)
                .where(and_(*conds))
                .order_by((distance + 0.0).asc().nullslast(), Photo.id.asc())
                .limit(cap + 1)
            )
            stmt = base.where(Photo.id.in_(candidates)).order_by(
                (distance + 0.0).asc().nullslast(), Photo.id.asc()
            )
        return stmt, inferred, structured, reliable

    async def recall(self, request, plan, vector, profile):
        stmt, inferred, structured, reliable = self.recall_statement(
            request, plan, vector, profile
        )
        if request.complete_result_set and not reliable:
            return [], False, inferred, structured, reliable
        if (
            settings.search_ann_enabled
            and vector is not None
            and request.retrieval_mode == "semantic"
            and request.result_mode != "select"
            and not request.complete_result_set
            and not structured
        ):
            version = (
                await self.db.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            ).scalar()
            if version and tuple(int(x) for x in version.split(".")[:2]) >= (0, 8):
                await self.db.execute(
                    text(
                        "SELECT set_config('hnsw.ef_search',:ef,true),set_config('hnsw.iterative_scan','strict_order',true),set_config('hnsw.max_scan_tuples',:scan,true)"
                    ),
                    {
                        "ef": str(settings.search_ann_ef_search),
                        "scan": str(settings.search_ann_max_scan_tuples),
                    },
                )
                conds, *_ = self.filters(request, plan.timezone)
                distance = Photo.embedding.cosine_distance(vector)
                nearest = (
                    select(Photo.id, distance.label("distance"))
                    .where(*conds)
                    .order_by(distance.asc())
                    .limit(settings.search_snapshot_max_candidates + 1)
                    .cte("nearest")
                    .prefix_with("MATERIALIZED")
                )
                ann_stmt = (
                    select(
                        *SEARCH_COLUMNS,
                        *score_expressions(request, plan, vector, profile),
                    )
                    .join(nearest, nearest.c.id == Photo.id)
                    .order_by(nearest.c.distance.asc(), Photo.id.asc())
                )
                rows = (await self.db.execute(ann_stmt)).mappings().all()
                self.approximate = len(rows) > settings.search_snapshot_max_candidates
                # Sparse filtered results cannot prove exhaustiveness through ANN.
                if not self.approximate:
                    rows = (await self.db.execute(stmt)).mappings().all()
            else:
                rows = (await self.db.execute(stmt)).mappings().all()
        else:
            rows = (await self.db.execute(stmt)).mappings().all()
        cap = settings.search_snapshot_max_candidates
        scored = [
            (
                SimpleNamespace(**{c.key: row[c.key] for c in SEARCH_COLUMNS}),
                row["sem"],
                row["rec"],
                row["inter"],
                row["final"],
            )
            for row in rows[:cap]
        ]
        if request.retrieval_mode != "timeline":
            scored.sort(key=lambda row: (-row[4], str(row[0].id)))
        return scored, len(rows) > cap, inferred, structured, reliable

    async def live(self, request, plan, rows, excluded=()):
        from uuid import UUID

        if not rows:
            return []
        conds, *_ = self.filters(request, plan.timezone, excluded=excluded)
        photos = (
            (
                await self.db.execute(
                    select(Photo)
                    .options(load_only(*SEARCH_COLUMNS, raiseload=True))
                    .where(*conds, Photo.id.in_([UUID(row["id"]) for row in rows]))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        by_id = {str(photo.id): photo for photo in photos}
        return [
            (row, by_id[row["id"]])
            for row in rows
            if row["id"] in by_id and row["version"] == photo_version(by_id[row["id"]])
        ]
