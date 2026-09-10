# ruff: noqa: F811
"""Shared SearchService contracts against isolated PostgreSQL and Redis."""

import asyncio
import os
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, update
from app.config import settings
from app.models.photo import Photo
from app.services import search_engine as engine, search_verification as verification
from app.services.search_contracts import SearchRequest, SearchError
from app.services.search_budget import (
    SearchBudget,
    BudgetExhausted,
    model_call,
    record_provider_usage,
)
from app.services.search_store import SearchStore, decode_cursor
from app.services.search_reranker import RerankDecision
from app.schemas.photo import ParsedQuery
from tests.test_batch1_integration import infra, add_photo  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]


@pytest.fixture
def models(monkeypatch):
    parser = AsyncMock(return_value=ParsedQuery(semantic="cat"))
    monkeypatch.setattr(engine, "parse_query", parser)
    monkeypatch.setattr(
        engine, "get_query_embedding", AsyncMock(return_value=([0.01] * 1024, False))
    )
    monkeypatch.setattr(settings, "search_visual_verify_enabled", False)
    monkeypatch.setattr(settings, "search_rerank_enabled", True)
    monkeypatch.setattr(settings, "search_rerank_require_match", True)
    return parser


async def photos(factory, user, count):
    return sorted(
        [
            await add_photo(factory, user, status="done", embedding=[0.01] * 1024)
            for _ in range(count)
        ]
    )


def request(**kw):
    return SearchRequest(
        q="cat",
        auto_parse=False,
        verify_constraints=False,
        w_semantic=1,
        w_recency=0,
        w_interaction=0,
        **kw,
    )


@pytest.mark.asyncio
async def test_equal_score_snapshot_pages_survive_insert_delete_and_rescore(
    infra, models
):
    factory, redis, user = infra
    ids = await photos(factory, user, 7)
    req = request(result_mode="select", limit=2)
    async with factory() as db:
        service = engine.SearchService(db, user)
        first = await service.search(req)
        assert [p["id"] for p in first["items"]] == ids[:2]
        await add_photo(factory, user, status="done", embedding=[0.01] * 1024)
        async with factory() as other:
            await other.execute(delete(Photo).where(Photo.id == UUID(ids[2])))
            await other.execute(
                update(Photo)
                .where(Photo.id == UUID(ids[3]))
                .values(ai_description="changed")
            )
            await other.commit()
        got = [p["id"] for p in first["items"]]
        cursor = first["next_cursor"]
        while cursor:
            page = await service.search(req.model_copy(update={"cursor": cursor}))
            got.extend(p["id"] for p in page["items"])
            cursor = page["next_cursor"]
        assert got == ids[:2] + ids[4:]
        assert (
            not page["search_exhausted"] and page["stop_reason"] == "snapshot_changed"
        )
        with pytest.raises(SearchError, match="cursor_query_mismatch"):
            await service.search(
                req.model_copy(update={"q": "dog", "cursor": first["next_cursor"]})
            )
        with pytest.raises(SearchError, match="invalid_cursor"):
            decode_cursor(first["next_cursor"], uuid4())
        with pytest.raises(SearchError):
            decode_cursor(first["next_cursor"] + "bad", user)
        raw = await redis.get(f"search:v3:plan:{user}:{first['search_id']}")
        assert "thumb_url" not in raw
        await redis.delete(f"search:v3:plan:{user}:{first['search_id']}")
        with pytest.raises(SearchError, match="search_expired"):
            await service.search(
                req.model_copy(update={"cursor": first["next_cursor"]})
            )


@pytest.mark.asyncio
async def test_reject_first_five_then_match_sixth_and_never_visual_when_disabled(
    infra, models, monkeypatch
):
    factory, _, user = infra
    ids = await photos(factory, user, 6)
    calls = []

    async def judge(q, evidence):
        calls.append(evidence)
        return [
            RerankDecision(
                e["candidate_key"],
                "match" if e["photo_id"] == ids[-1] else "contradiction",
                1,
                "",
            )
            for e in evidence
        ], {}

    monkeypatch.setattr(verification, "judge_candidate_evidence", judge)
    visual = AsyncMock(side_effect=AssertionError("visual disabled"))
    monkeypatch.setattr(verification, "judge_visual_candidates", visual)
    async with factory() as db:
        result = await engine.SearchService(db, user).search(
            request(result_mode="best", force_visual_verify=True)
        )
    assert [p["id"] for p in result["items"]] == ids[-1:]
    assert result["items"][0]["verification_status"] == "match"
    assert len(calls) == 2 and [len(x) for x in calls] == [5, 1]
    visual.assert_not_called()


@pytest.mark.asyncio
async def test_parser_once_and_budget_shared_with_prefetch_and_fallback(
    infra, models, monkeypatch
):
    factory, redis, user = infra
    await photos(factory, user, 12)
    monkeypatch.setattr(settings, "search_max_model_calls", 1)

    async def judge(q, evidence):
        async def response():
            return [
                RerankDecision(e["candidate_key"], "match", 1, "") for e in evidence
            ], {}

        return await model_call("text", response)

    monkeypatch.setattr(verification, "judge_candidate_evidence", judge)
    req = request(result_mode="best").model_copy(update={"auto_parse": True})
    async with factory() as db:
        service = engine.SearchService(db, user)
        first = await service.search(req)
        plan = await SearchStore(redis).load(user, first["search_id"])
        more = await service.search(
            req.model_copy(
                update={"cursor": first["next_cursor"], "candidate_pool_size": 12}
            ),
            plan_id=first["search_id"],
            prefetch=True,
        )
        assert len(more["items"]) == 4
        assert (
            more["stop_reason"] == "budget_exhausted" and not more["search_exhausted"]
        )
        fallback = await service.fallback(req, plan_id=first["search_id"])
        assert fallback["search_usage"]["calls"] == 1
        assert more["search_usage"]["calls"] == 1
        assert plan["plan"]["budget_id"] == first["search_id"]
    models.assert_awaited_once()


@pytest.mark.asyncio
async def test_verifier_failure_is_not_exhaustion(infra, models, monkeypatch):
    factory, _, user = infra
    await photos(factory, user, 3)
    monkeypatch.setattr(
        verification,
        "judge_candidate_evidence",
        AsyncMock(side_effect=ConnectionError()),
    )
    async with factory() as db:
        result = await engine.SearchService(db, user).search(request())
    assert result["items"] == [] and not result["search_exhausted"]
    assert (
        result["stop_reason"] == "verification_unavailable"
        and result["unverified_count"] == 3
    )


@pytest.mark.asyncio
async def test_atomic_budget_cap_deadline_and_no_expired_usage_resurrection(
    infra, monkeypatch
):
    _, redis, user = infra
    monkeypatch.setattr(settings, "search_max_model_calls", 2)
    budget = SearchBudget(redis, user, uuid4())
    await budget.create()
    results = await asyncio.gather(
        *(budget.reserve("text") for _ in range(8)), return_exceptions=True
    )
    assert sum(isinstance(x, float) for x in results) == 2
    assert sum(isinstance(x, BudgetExhausted) for x in results) == 6
    # Legacy hashes alone retain the original absolute deadline.
    await redis.hdel(budget.key, "schema_version", "expires_at")
    await redis.hset(budget.key, "deadline", 0)
    with pytest.raises(BudgetExhausted, match="deadline_exceeded"):
        await budget.reserve("text")
    await redis.delete(budget.key)
    with budget.activate():
        await record_provider_usage({"total_tokens": 100})
    assert not await redis.exists(budget.key)


@pytest.mark.asyncio
async def test_cancel_preserves_progress_and_stale_owner_cannot_write(infra, models):
    factory, redis, user = infra
    async with factory() as db:
        plan = await engine.SearchService(db, user).create_plan(
            request(result_mode="select")
        )
    store = SearchStore(redis)
    with pytest.raises(asyncio.CancelledError):
        async with store.mutation(user, plan.id) as snapshot:
            snapshot["scan"] = 7
            raise asyncio.CancelledError()
    assert (await store.load(user, plan.id))["scan"] == 7
    with pytest.raises(SearchError, match="search_ownership_lost"):
        async with store.mutation(user, plan.id) as snapshot:
            snapshot["scan"] = 9
            await redis.set(
                f"search:v3:plan:{user}:{plan.id}:lock", "replacement", ex=30
            )
    assert (await store.load(user, plan.id))["scan"] == 7
    assert await redis.get(f"search:v3:plan:{user}:{plan.id}:lock") == "replacement"


@pytest.mark.asyncio
async def test_json_or_filters_partial_status_and_candidate_cap(
    infra, models, monkeypatch
):
    factory, _, user = infra
    await add_photo(
        factory,
        user,
        status="partial_done",
        embedding=[0.01] * 1024,
        ai_analysis={"objects": ["cat"]},
    )
    await add_photo(
        factory,
        user,
        status="done",
        embedding=[0.01] * 1024,
        ai_analysis={"scene": "park"},
    )
    await add_photo(
        factory,
        user,
        status="done",
        embedding=[0.01] * 1024,
        ai_analysis={"objects": ["dog"]},
    )
    async with factory() as db:
        service = engine.SearchService(db, user)
        result = await service.search(
            request(result_mode="select", objects=["cat"], scene="park")
        )
        assert len(result["items"]) == 2
        monkeypatch.setattr(settings, "search_snapshot_max_candidates", 2)
        result = await service.search(request(result_mode="select"))
        assert result["truncated"] and result["stop_reason"] == "candidate_limit"
        assert not result["search_exhausted"]


@pytest.mark.asyncio
async def test_worker_consumes_exact_http_plan(infra, models, monkeypatch):
    from types import SimpleNamespace
    from app.api.search import semantic_search
    from app.schemas.photo import SearchQuery
    from app.workers import search_tasks
    from app.services import search_candidate_pool as pools

    factory, _, user = infra
    ids = await photos(factory, user, 8)

    async def judge(q, evidence):
        return [
            RerankDecision(e["candidate_key"], "match", 1, "") for e in evidence
        ], {}

    monkeypatch.setattr(verification, "judge_candidate_evidence", judge)
    async with factory() as db:
        result = await semantic_search(
            SearchQuery(
                q="cat",
                result_mode="best",
                verify_constraints=False,
                w_semantic=1,
                w_recency=0,
                w_interaction=0,
            ),
            SimpleNamespace(id=user),
            db,
        )
    scope = await pools.begin_candidate_search(user, uuid4())
    worked = await search_tasks.prefetch_search_candidates(
        {},
        scope,
        str(user),
        "ignored worker query",
        [ids[0]],
        {"plan_id": result.search_id, "cursor": result.next_cursor},
    )
    assert worked["ok"] and worked["verified_count"] == 7
    got = []
    while item := await pools.pop_verified_candidate(scope):
        assert item["verification_status"] == "match" and "thumb_url" not in item
        got.append(item["id"])
    assert got == ids[1:]
    models.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_derived_plan_preserves_parsed_dates_budget_and_timezone(
    infra, models
):
    from datetime import date
    from app.services.search_time import _zone

    factory, redis, user = infra
    models.return_value = ParsedQuery(
        semantic="cat", from_date=date(2026, 9, 5), to_date=date(2026, 9, 5)
    )
    token = _zone.set("Asia/Shanghai")
    try:
        async with factory() as db:
            service = engine.SearchService(db, user)
            plan = await service.create_plan(
                request().model_copy(
                    update={"auto_parse": True, "q": "今天拍摄的cat照片"}
                )
            )
            derived = await service.derive_plan(plan, {"status": None})
            assert (
                derived.request().from_date
                == plan.request().from_date
                == date(2026, 9, 5)
            )
            assert derived.timezone == plan.timezone == "Asia/Shanghai"
            assert (
                derived.budget_id == plan.budget_id
                and derived.scoring_time == plan.scoring_time
            )
            assert derived.parsed_json == plan.parsed_json
            async with SearchStore(redis).mutation(user, plan.id):
                with pytest.raises(SearchError, match="search_busy"):
                    await service.search(request(), plan_id=plan.id)
    finally:
        _zone.reset(token)
    models.assert_awaited_once()
