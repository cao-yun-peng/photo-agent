"""SearchService: the only owner of parsing, retrieval, verification and pagination."""

import asyncio
import json
from functools import wraps
from uuid import UUID, uuid4
from app.config import settings
from app.services.lock import get_redis
from app.services.search_contracts import (
    SearchRequest,
    QueryPlan,
    SearchError,
    request_fingerprint,
)
from app.services.search_budget import (
    SearchBudget,
    BudgetExhausted,
    RequestTimeout,
    search_execution,
    execution_remaining,
)
from app.services.search_store import SearchStore, encode_cursor, decode_cursor
from app.services.search_repository import SearchRepository, photo_version
from app.services.search_verification import verify_batch
from app.services.query_parser import parse_query, resolve_auto_parsed_query
from app.services.search_time import current_timezone, local_today, utc_now
from app.services.search import (
    get_query_embedding,
    get_user_profile,
    resolve_semantic_threshold,
    apply_semantic_threshold,
    build_search_coverage_hint,
)
from app.services.search_constraints import (
    extract_structured_constraints,
    validate_scored_candidates,
)
from app.services.search_index import get_index_coverage
from app.services.oss import sign_get_url


def serialize_photo(photo, scores, verdict="unverified"):
    return {
        "id": str(photo.id),
        "thumb_url": sign_get_url(photo.thumb_key or photo.oss_key),
        "taken_at": photo.taken_at.isoformat() if photo.taken_at else None,
        "ai_description": photo.ai_description,
        "status": photo.status,
        "ai_analysis": photo.ai_analysis or {},
        "verification_status": verdict,
        "_search_version": photo_version(photo),
        **{
            name: round(value, 4)
            for name, value in zip(
                ["score_semantic", "score_recency", "score_interaction", "score_final"],
                scores,
            )
        },
    }


def _row(item, verdict="unverified"):
    return {
        "id": str(item[0].id),
        "scores": list(item[1:]),
        "version": photo_version(item[0]),
        "verdict": verdict,
    }


def _merge_summary(target, batch):
    for key, value in batch.items():
        if isinstance(value, int) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value
        elif key == "degraded":
            target[key] = target.get(key, False) or value
        elif value is not None:
            target[key] = value


def _in_execution(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        with search_execution():
            return await method(*args, **kwargs)

    return wrapped


async def _budgeted(operation, budget):
    remaining = await budget.remaining()
    if remaining <= 0:
        raise RequestTimeout()
    timeout = asyncio.timeout(remaining)
    try:
        async with timeout:
            return await operation()
    except TimeoutError:
        if not timeout.expired():
            raise
        await budget.remaining()
        raise RequestTimeout() from None


class SearchService:
    def __init__(self, db, user_id):
        self.db, self.user_id = db, UUID(str(user_id))
        self.repository = SearchRepository(db, self.user_id)

    @_in_execution
    async def create_plan(self, request, *, budget_id=None):
        redis = await get_redis()
        store = SearchStore(redis)
        plan_id = uuid4()
        budget = SearchBudget(redis, self.user_id, budget_id or plan_id)
        if budget_id is None:
            expires_at = await budget.create()
        else:
            expires_at = await budget.expires_at()
        timezone = current_timezone()
        parsed = None
        effective = request.q.strip()
        accepted = request
        with budget.activate():
            if request.auto_parse and request.retrieval_mode == "semantic":
                parsed = await parse_query(request.q)
                effective, start, end = resolve_auto_parsed_query(
                    request.q,
                    parsed,
                    from_date=request.from_date,
                    to_date=request.to_date,
                )
                accepted = request.model_copy(
                    update={"from_date": start, "to_date": end}
                )
            # Selection ownership never weakens a semantic search's evidence gate.
            verification = "strict" if request.retrieval_mode == "semantic" else "off"
            plan = QueryPlan(
                id=plan_id,
                user_id=self.user_id,
                raw_query=request.q,
                effective_query=effective,
                timezone=timezone,
                local_date=local_today(),
                scoring_time=utc_now().isoformat(),
                request_json=accepted.model_dump_json(),
                parsed_json=parsed.model_dump_json() if parsed else None,
                fingerprint=request_fingerprint(request, timezone),
                verification=verification,
                allow_visual=bool(settings.search_visual_verify_enabled),
                budget_id=budget_id or plan_id,
                expires_at=expires_at,
            )
        await store.create(
            {
                "plan": plan.model_dump(mode="json"),
                "rows": None,
                "accepted": [],
                "scan": 0,
                "terminal": None,
                "rerank": {},
                "meta": {},
            }
        )
        return plan

    async def _initialize(self, snapshot, plan, budget):
        request = plan.request()
        vector, cache_hit = (None, False)
        if request.retrieval_mode != "timeline" and plan.effective_query:
            vector, cache_hit = await get_query_embedding(plan.effective_query)
        profile = await get_user_profile(self.db, self.user_id)
        scored, capped, inferred, structured, reliable = await self.repository.recall(
            request, plan, vector, profile
        )
        constraints = (
            extract_structured_constraints(plan.raw_query)
            if request.verify_constraints
            else []
        )
        scored, constraint_check = validate_scored_candidates(scored, constraints)
        threshold, bypass = resolve_semantic_threshold(
            request.min_semantic_score, structured_collection=structured
        )
        if request.retrieval_mode != "semantic":
            threshold = None
        scored, filtered = apply_semantic_threshold(scored, threshold)
        coverage = (
            await get_index_coverage(self.db, self.user_id)
            if (
                request.include_index_coverage
                or request.complete_result_set
                or structured
            )
            else None
        )
        snapshot["rows"] = [_row(item) for item in scored]
        snapshot["meta"] = {
            "cache_hit": cache_hit,
            "constraint_check": constraint_check,
            "index_coverage": coverage,
            "inferred_filters": inferred,
            "similarity_threshold": threshold,
            "threshold_filtered_count": filtered,
            "threshold_bypassed_reason": bypass,
            "semantic_facets_required": structured,
            "candidate_cap_reached": capped,
            "approximate_recall": self.repository.approximate,
            "scope_reliable": reliable,
        }
        if request.complete_result_set and not reliable:
            snapshot["terminal"] = "semantic_scope_unverified"

    @_in_execution
    async def search(self, request: SearchRequest, *, plan_id=None, prefetch=False):
        redis = await get_redis()
        store = SearchStore(redis)
        offset = 0
        if request.cursor:
            cursor_plan, offset = decode_cursor(request.cursor, self.user_id)
            if plan_id and str(plan_id) != cursor_plan:
                raise SearchError("cursor_plan_mismatch", 400)
            snapshot = await store.load(self.user_id, cursor_plan)
            plan = QueryPlan.model_validate(snapshot["plan"])
            if (
                plan_id is None
                and request_fingerprint(request, current_timezone()) != plan.fingerprint
            ):
                raise SearchError("cursor_query_mismatch", 400)
        elif plan_id:
            snapshot = await store.load(self.user_id, plan_id)
            plan = QueryPlan.model_validate(snapshot["plan"])
        else:
            plan = await self.create_plan(request)
        # Existing plans own all filters and policy. Only exclusions and page size evolve.
        policy = plan.request()
        if policy.retrieval_mode == "semantic" and plan.verification != "strict":
            # Old snapshots may already contain accepted but unverified photos.
            raise SearchError("search_verification_policy_changed")
        if (
            policy.retrieval_mode in {"timeline", "album"}
            and policy.result_mode == "select"
        ):
            if prefetch:
                raise SearchError("prefetch_requires_verified_plan", 400)
            budget = SearchBudget(redis, self.user_id, plan.budget_id)
            timeout = None
            try:
                with budget.activate():
                    timeout = asyncio.timeout(execution_remaining())
                    async with timeout:
                        return await self._search_album(
                            request, plan, offset, store, budget
                        )
            except RequestTimeout:
                pass
            except TimeoutError:
                if timeout is None or not timeout.expired():
                    raise
            except BudgetExhausted as exc:
                return await self._pending_result(
                    plan,
                    budget,
                    offset,
                    exc.code,
                    resumable=False,
                )
            return await self._pending_result(plan, budget, offset, "request_timeout")
        if not isinstance(offset, int):
            raise SearchError("invalid_cursor_offset", 400)
        if prefetch and plan.verification != "strict":
            raise SearchError("prefetch_requires_verified_plan", 400)
        budget = SearchBudget(redis, self.user_id, plan.budget_id)
        limit = (
            min(request.candidate_pool_size, 100)
            if prefetch
            else 1
            if policy.result_mode == "best"
            else min(request.limit, 5)
            if policy.result_mode == "browse"
            else min(request.limit, settings.search_snapshot_max_candidates)
        )
        if policy.complete_result_set:
            limit = min(request.limit, settings.search_snapshot_max_candidates)
        return_verified_batch = (
            settings.search_browse_early_return
            and not prefetch
            and policy.result_mode == "browse"
            and plan.verification == "strict"
            and not policy.complete_result_set
        )
        async with store.mutation(self.user_id, plan.id) as snapshot:
            with budget.activate():
                if offset > len(snapshot["accepted"]):
                    raise SearchError("invalid_cursor_offset", 400)
                page = {"position": offset, "items": []}
                paused = None
                timeout = None
                try:
                    timeout = asyncio.timeout(execution_remaining())
                    async with timeout:
                        await self._collect_page(
                            snapshot,
                            plan,
                            request,
                            budget,
                            limit,
                            return_verified_batch,
                            page,
                        )
                except RequestTimeout:
                    paused = "request_timeout"
                except TimeoutError:
                    if timeout is None or not timeout.expired():
                        raise
                    paused = "request_timeout"
                except BudgetExhausted as exc:
                    snapshot["terminal"] = exc.code
                position, items = page["position"], page["items"]
                if snapshot["rows"] is None:
                    return await self._pending_result(
                        plan,
                        budget,
                        position,
                        snapshot["terminal"] or paused or "request_timeout",
                        resumable=not snapshot["terminal"],
                    )
                has_more = position < len(snapshot["accepted"]) or (
                    not snapshot["terminal"]
                    and snapshot["scan"] < len(snapshot["rows"])
                )
                cursor = (
                    encode_cursor(self.user_id, plan.id, position, plan.expires_at)
                    if has_more
                    else None
                )
                meta = snapshot["meta"]
                all_scanned = snapshot["scan"] >= len(snapshot["rows"])
                coverage = meta.get("index_coverage")
                coverage_complete = bool(
                    coverage
                    and coverage.get("complete")
                    and (
                        coverage.get("semantic_complete")
                        if meta["semantic_facets_required"]
                        else True
                    )
                )
                complete = bool(
                    policy.complete_result_set
                    and all_scanned
                    and not cursor
                    and not meta["candidate_cap_reached"]
                    and not snapshot["terminal"]
                    and not snapshot.get("invalidated")
                    and coverage_complete
                    and not snapshot["rerank"].get("uncertain_count")
                    and meta["scope_reliable"]
                )
                if complete:
                    reason = None
                elif not policy.complete_result_set:
                    reason = "not_requested"
                else:
                    reason = (
                        snapshot["terminal"]
                        or paused
                        or (
                            "candidate_limit"
                            if meta["candidate_cap_reached"]
                            else "more_pages"
                            if cursor
                            else "index_incomplete"
                            if not coverage_complete
                            else "semantic_scope_unverified"
                        )
                    )
                stop = (
                    snapshot["terminal"]
                    or paused
                    or (
                        "verified_batch_ready"
                        if has_more and return_verified_batch and 0 < len(items) < limit
                        else "page_full"
                        if has_more
                        else "candidate_limit"
                        if meta["candidate_cap_reached"]
                        else "snapshot_changed"
                        if snapshot.get("invalidated")
                        else "verification_incomplete"
                        if snapshot["rerank"].get("uncertain_count")
                        else "candidates_exhausted"
                    )
                )
                result = {
                    **meta,
                    "items": items,
                    "total": len(items),
                    "total_matches": len(snapshot["accepted"]),
                    "result_mode": policy.result_mode,
                    "result_set_complete": complete,
                    "completeness_reason": reason,
                    "truncated": has_more or meta["candidate_cap_reached"],
                    "next_cursor": cursor,
                    "parsed": json.loads(plan.parsed_json)
                    if plan.parsed_json
                    else None,
                    "rerank_check": snapshot["rerank"] or None,
                    "stop_reason": stop,
                    "search_pending": paused is not None,
                    "unverified_count": len(snapshot["rows"])
                    - snapshot["scan"]
                    + snapshot["rerank"].get("unverified_count", 0)
                    + len(snapshot.get("invalidated", [])),
                    "coverage_hint": build_search_coverage_hint(
                        coverage,
                        requires_facets=meta["semantic_facets_required"],
                        threshold=meta["similarity_threshold"],
                        threshold_filtered_count=meta["threshold_filtered_count"],
                    ),
                    "search_id": str(plan.id),
                    "_search_plan_id": str(plan.id),
                    "search_usage": await budget.usage(),
                    "search_exhausted": bool(
                        all_scanned
                        and not cursor
                        and coverage_complete
                        and not meta["candidate_cap_reached"]
                        and not snapshot["terminal"]
                        and not snapshot.get("invalidated")
                        and not snapshot["rerank"].get("uncertain_count")
                    ),
                    "selection_owner": "system"
                    if policy.result_mode == "best"
                    else "user",
                }
        return result

    async def _search_album(self, request, plan, offset, store, budget):
        from app.services.search_album import album_page

        policy = plan.request()
        context = None
        if policy.retrieval_mode == "album":
            async with store.mutation(self.user_id, plan.id) as snapshot:
                if "album_context" not in snapshot:
                    snapshot["album_context"] = await _budgeted(
                        lambda: self._album_context(plan), budget
                    )
                context = snapshot["album_context"]
        result = await album_page(self, request, plan, offset, context)
        result["search_usage"] = await budget.usage()
        return result

    async def _album_context(self, plan):
        vector, _ = (
            await get_query_embedding(plan.effective_query)
            if plan.effective_query
            else (None, False)
        )
        profile = await get_user_profile(self.db, self.user_id)
        return {
            "vector": list(map(float, vector)) if vector is not None else None,
            "profile": {
                "tag_affinity": profile.tag_affinity or {},
                "style_distribution": list(map(float, profile.style_distribution))
                if profile.style_distribution is not None
                else None,
            }
            if profile
            else None,
        }

    async def _collect_page(
        self, snapshot, plan, request, budget, limit, return_verified_batch, page
    ):
        policy = plan.request()
        items = page["items"]
        if snapshot["rows"] is None:
            if snapshot["terminal"]:
                return
            await _budgeted(lambda: self._initialize(snapshot, plan, budget), budget)
        # Each accepted index is immutable; new verification appends only.
        while len(items) < limit:
            available = snapshot["accepted"][page["position"] :]
            if available:
                live = await self.repository.live(
                    policy, plan, available, request.exclude_photo_ids
                )
                live_by_id = {entry["id"]: (entry, photo) for entry, photo in live}
                for entry in available:
                    page["position"] += 1
                    current = live_by_id.get(entry["id"])
                    if current:
                        item, photo = current
                        items.append(
                            serialize_photo(photo, item["scores"], item["verdict"])
                        )
                    elif entry["id"] not in {str(x) for x in request.exclude_photo_ids}:
                        snapshot["invalidated"] = sorted(
                            set(snapshot.get("invalidated", [])) | {entry["id"]}
                        )
                    if len(items) >= limit:
                        break
            if (
                len(items) >= limit
                or (return_verified_batch and items)
                or snapshot["terminal"]
                or snapshot["scan"] >= len(snapshot["rows"])
            ):
                break
            size = max(1, settings.search_rerank_top_k)
            batch_rows = snapshot["rows"][snapshot["scan"] : snapshot["scan"] + size]
            live = await self.repository.live(policy, plan, batch_rows)
            live_ids = {entry["id"] for entry, _ in live}
            snapshot["invalidated"] = sorted(
                set(snapshot.get("invalidated", []))
                | {entry["id"] for entry in batch_rows if entry["id"] not in live_ids}
            )
            scored = [(photo, *entry["scores"]) for entry, photo in live]
            if scored:
                if plan.verification == "off":
                    accepted, summary = await verify_batch(scored, plan, budget)
                else:
                    accepted, summary = await _budgeted(
                        lambda: verify_batch(scored, plan, budget), budget
                    )
                snapshot["accepted"].extend(
                    _row(item, verdict) for item, verdict in accepted
                )
                _merge_summary(snapshot["rerank"], summary)
                if summary.get("unverified_count"):
                    snapshot["terminal"] = "verification_unavailable"
            snapshot["scan"] += len(batch_rows)

    async def _pending_result(self, plan, budget, offset, reason, *, resumable=True):
        return {
            "items": [],
            "total": 0,
            "total_matches": 0,
            "result_mode": plan.request().result_mode,
            "result_set_complete": False,
            "completeness_reason": reason,
            "stop_reason": reason,
            "search_pending": resumable,
            "search_exhausted": False,
            "truncated": resumable,
            "next_cursor": encode_cursor(self.user_id, plan.id, offset, plan.expires_at)
            if resumable
            else None,
            "search_id": str(plan.id),
            "_search_plan_id": str(plan.id),
            "search_usage": await budget.usage(),
            "parsed": json.loads(plan.parsed_json) if plan.parsed_json else None,
        }

    @_in_execution
    async def derive_plan(self, parent, updates):
        """Fallback may relax declared filters; never creates a new parsing or call budget."""
        request = parent.request().model_copy(
            update={**updates, "cursor": None, "auto_parse": False}
        )
        plan = parent.model_copy(
            update={
                "id": uuid4(),
                "request_json": request.model_dump_json(),
                "verification": "strict"
                if request.retrieval_mode == "semantic"
                else "off",
                "allow_visual": parent.allow_visual and request.verify_semantic,
            }
        )
        store = SearchStore(await get_redis())
        await store.create(
            {
                "plan": plan.model_dump(mode="json"),
                "rows": None,
                "accepted": [],
                "scan": 0,
                "terminal": None,
                "rerank": {},
                "meta": {},
            }
        )
        return plan

    @_in_execution
    async def feedback_search(self, request, *, plan_id, level, cursor=None):
        """Feedback preserves semantic evidence; browsing needs an explicit action.

        Derived plans retain ownership, exclusions and the parent budget.
        """
        store = SearchStore(await get_redis())
        parent = QueryPlan.model_validate(
            (await store.load(self.user_id, plan_id))["plan"]
        )
        if cursor or level >= 2:
            return {
                "items": [],
                "ok": False,
                "error_type": "scope_requires_user",
                "browse_scope": "matches",
                "fallback_level": 1,
                "hint": "仍会按原条件核验；如需浏览未确认匹配的照片，请明确要求浏览相册。",
            }
        updates = {
            "verify_semantic": True,
            "verify_constraints": True,
            "verified_only": True,
        }
        derived = await self.derive_plan(parent, updates)
        # Turning verification on must not widen the parent's visual capability.
        result = await self.search(request, plan_id=derived.id)
        return {
            **result,
            "browse_scope": "clues" if level >= 2 else "matches",
            "fallback_level": 2 if level >= 2 else 1,
        }

    @_in_execution
    async def fallback(
        self, request, *, plan_id=None, start_level=0, allow_unfiltered=True
    ):
        store = SearchStore(await get_redis())
        plan = (
            QueryPlan.model_validate((await store.load(self.user_id, plan_id))["plan"])
            if plan_id
            else await self.create_plan(request)
        )
        if start_level <= 0:
            result = await self.search(request, plan_id=plan.id)
            if result["items"] or result["stop_reason"] in {
                "budget_exhausted",
                "deadline_exceeded",
                "request_timeout",
                "verification_unavailable",
                "verification_incomplete",
            }:
                return {**result, "fallback_level": 0}
        relaxed = await self.derive_plan(plan, {"status": None})
        result = await self.search(request, plan_id=relaxed.id)
        result["fallback_level"] = 1
        return result
