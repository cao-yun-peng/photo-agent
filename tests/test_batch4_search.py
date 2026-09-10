# ruff: noqa: F811
"""SQL scoring, live keysets and distributed cache ownership regressions."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4, UUID
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, update
from app.config import settings
from app.models.photo import Photo
from app.services.search_repository import SearchRepository
from app.services.search_contracts import SearchRequest, SearchError
from app.services.search_budget import SearchBudget, model_call
from app.services.search_cache import cached_call, cache_key
from app.services.search import (
    semantic_score,
    recency_score,
    personalized_interaction_score,
    combine,
)
from tests.test_batch1_integration import infra, add_photo  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"), reason="isolated DB required"
    ),
]


@pytest.mark.asyncio
async def test_sql_scores_equal_python_with_profile_nulls_and_future(
    infra, monkeypatch
):
    factory, _, user = infra
    now = datetime.now(timezone.utc)
    vectors = [
        [1.0] + [0.0] * 1023,
        [-1.0] + [0.0] * 1023,
        None,
        [0.0, 1.0] + [0.0] * 1022,
        [0.0] * 1024,
    ]
    for index, vector in enumerate(vectors):
        await add_photo(
            factory,
            user,
            status="done",
            embedding=vector,
            taken_at=None if index == 0 else now + timedelta(days=index - 2),
            ai_analysis={"objects": ["cat", "cat"], "scene": "cat", "mood": "happy"},
        )
    profile = SimpleNamespace(
        tag_affinity={"cat": 2.0, "happy": 0.3}, style_distribution=vectors[0]
    )
    req = SearchRequest(q="cat", retrieval_mode="album", result_mode="select")
    plan = SimpleNamespace(timezone="UTC", scoring_time=now.isoformat())
    async with factory() as db:
        scored, *_ = await SearchRepository(db, user).recall(
            req, plan, vectors[0], profile
        )
        originals = (
            await db.execute(
                select(Photo, Photo.embedding.cosine_distance(vectors[0])).where(
                    Photo.user_id == user
                )
            )
        ).all()
        expected = {}
        for photo, distance in originals:
            view = SimpleNamespace(
                ai_analysis=photo.ai_analysis,
                embedding=photo.embedding.tolist()
                if photo.embedding is not None
                else None,
            )
            sem = semantic_score(distance) if distance is not None else 0.0
            rec = recency_score(photo.taken_at, now=now)
            inter = personalized_interaction_score(profile, view)
            expected[str(photo.id)] = (
                sem,
                rec,
                inter,
                combine(sem, rec, inter, 0.7, 0.2, 0.1),
            )
        for photo, *scores in scored:
            assert scores == pytest.approx(expected[str(photo.id)], abs=1e-7)
            assert not hasattr(photo, "embedding")


@pytest.mark.asyncio
async def test_album_ranks_global_before_cap(infra, monkeypatch):
    factory, _, user = infra
    monkeypatch.setattr(settings, "search_snapshot_max_candidates", 2)
    now = datetime.now(timezone.utc)
    for _ in range(3):
        await add_photo(
            factory,
            user,
            status="done",
            embedding=[1.0] + [0.0] * 1023,
            taken_at=now - timedelta(days=300),
        )
    newest = await add_photo(
        factory, user, status="done", embedding=[-1.0] + [0.0] * 1023, taken_at=now
    )
    req = SearchRequest(
        q="cat",
        retrieval_mode="album",
        w_semantic=0.01,
        w_recency=0.99,
        w_interaction=0,
    )
    async with factory() as db:
        rows, capped, *_ = await SearchRepository(db, user).recall(
            req,
            SimpleNamespace(timezone="UTC", scoring_time=now.isoformat()),
            [1.0] + [0.0] * 1023,
            None,
        )
    assert str(rows[0][0].id) == newest and capped


@pytest.mark.asyncio
async def test_timeline_keyset_past_cap_nulls_insert_edit_and_tenant(
    infra, monkeypatch
):
    from app.services.search_engine import SearchService

    factory, _, user = infra
    monkeypatch.setattr(settings, "search_snapshot_max_candidates", 2)
    now = datetime.now(timezone.utc) - timedelta(days=1)
    ids = [
        await add_photo(factory, user, status="done", taken_at=now if i < 4 else None)
        for i in range(8)
    ]
    req = SearchRequest(q=" ", retrieval_mode="timeline", result_mode="select", limit=2)
    async with factory() as db:
        service = SearchService(db, user)
        page = await service.search(req)
        assert page["total_matches"] == 8
        got = [x["id"] for x in page["items"]]
        cursor = page["next_cursor"]
        with pytest.raises(SearchError):
            await SearchService(db, uuid4()).search(
                req.model_copy(update={"cursor": cursor})
            )
        await add_photo(factory, user, status="done", taken_at=now)
        unseen = next(x for x in ids if x not in got)
        async with factory() as other:
            await other.execute(
                update(Photo)
                .where(Photo.id == UUID(unseen))
                .values(taken_at=now + timedelta(days=1))
            )
            await other.commit()
        while cursor:
            page = await service.search(req.model_copy(update={"cursor": cursor}))
            got.extend(x["id"] for x in page["items"])
            cursor = page["next_cursor"]
        assert set(got) == set(ids) - {unseen} and len(got) == len(set(got))
        assert page["total_matches"] == 7


@pytest.mark.asyncio
async def test_singleflight_twenty_budgets_one_provider_call(infra):
    _, redis, user = infra
    budgets = [SearchBudget(redis, user, uuid4()) for _ in range(20)]
    await asyncio.gather(*(b.create() for b in budgets))
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return {"value": 42}

    async def request(budget):
        with budget.activate():
            return await cached_call(
                cache_key("test", ["same"]), lambda: model_call("text", provider)
            )

    results = await asyncio.gather(*(request(b) for b in budgets))
    assert calls == 1 and sum(not hit for _, hit in results) == 1
    assert sum([(await b.usage())["calls"] for b in budgets]) == 1
    assert all(value == {"value": 42} for value, _ in results)


@pytest.mark.asyncio
async def test_waiter_cancel_does_not_cancel_owner(infra):
    _, _, user = infra
    key = "search:v4:cache:test:" + str(user)
    entered, release = asyncio.Event(), asyncio.Event()

    async def compute():
        entered.set()
        await release.wait()
        return 9

    owner = asyncio.create_task(cached_call(key, compute))
    await entered.wait()
    follower = asyncio.create_task(
        cached_call(key, AsyncMock(side_effect=AssertionError()))
    )
    await asyncio.sleep(0.04)
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    release.set()
    assert await owner == (9, False)


@pytest.mark.asyncio
async def test_owner_cancel_allows_waiter_recovery_and_fences_stale_write(infra):
    _, redis, user = infra
    key = "search:v4:cache:test:" + str(user)
    entered = asyncio.Event()

    async def compute():
        entered.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(cached_call(key, compute))
    await entered.wait()
    follower = asyncio.create_task(cached_call(key, AsyncMock(return_value=7)))
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert await follower == (7, False)
    await redis.delete(key)

    async def stale():
        await redis.set(key + ":flight", "replacement", ex=30)
        return 8

    with pytest.raises(SearchError, match="cache_ownership_lost"):
        await cached_call(key, stale)
    assert (
        not await redis.exists(key)
        and await redis.get(key + ":flight") == "replacement"
    )


@pytest.mark.asyncio
async def test_failures_coalesce_without_caching_success(infra):
    _, redis, user = infra
    key = "search:v4:cache:test:" + str(user)
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.04)
        raise ConnectionError("synthetic")

    results = await asyncio.gather(
        *(cached_call(key, fail) for _ in range(12)), return_exceptions=True
    )
    assert calls == 1 and all(isinstance(x, Exception) for x in results)
    assert not await redis.exists(key)


@pytest.mark.asyncio
async def test_cache_scope_model_photo_and_revision(infra, monkeypatch):
    from app.services import search_reranker as judge

    _, redis, user = infra
    b = SearchBudget(redis, user, uuid4())
    await b.create()
    calls = 0

    async def provider(q, candidates, **kw):
        nonlocal calls
        calls += 1
        return [judge.RerankDecision("c0", "match", 1, "synthetic")], {}

    monkeypatch.setattr(judge, "_judge_candidate_evidence", provider)
    candidates = [
        {
            "candidate_key": "c0",
            "photo_id": str(uuid4()),
            "analysis": {"objects": ["cat"]},
        }
    ]
    with b.activate():
        await judge.judge_candidate_evidence("cat", candidates)
        assert (await judge.judge_candidate_evidence("cat", candidates))[1]["cache_hit"]
        candidates[0]["analysis"]["objects"] = ["dog"]
        await judge.judge_candidate_evidence("cat", candidates)
        monkeypatch.setattr(settings, "search_rerank_model", "different-test-model")
        await judge.judge_candidate_evidence("cat", candidates)
        monkeypatch.setattr(settings, "search_cache_revision", "bumped")
        await judge.judge_candidate_evidence("cat", candidates)
    other = SearchBudget(redis, uuid4(), uuid4())
    await other.create()
    with other.activate():
        await judge.judge_candidate_evidence("cat", candidates)
    assert calls == 5


@pytest.mark.asyncio
async def test_parser_cache_local_day_and_budget(infra, monkeypatch):
    from app.services import query_parser as parser

    _, redis, user = infra
    b = SearchBudget(redis, user, uuid4())
    await b.create()
    monkeypatch.setattr(parser, "_is_mock", lambda: False)
    llm = AsyncMock(side_effect=lambda text, **kw: parser._rule_based_parse(text, **kw))
    monkeypatch.setattr(parser, "_llm_parse", llm)
    with b.activate():
        for _ in range(3):
            await parser.parse_query(
                "今天拍的猫",
                timezone_name="Asia/Shanghai",
                clock=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
            )
        await parser.parse_query(
            "今天拍的猫",
            timezone_name="Asia/Shanghai",
            clock=lambda: datetime(2026, 9, 6, tzinfo=timezone.utc),
        )
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_ann_opt_in_sparse_exact_fallback(infra, monkeypatch):
    factory, _, user = infra
    for _ in range(3):
        await add_photo(factory, user, status="done", embedding=[1.0] + [0.0] * 1023)
    monkeypatch.setattr(settings, "search_ann_enabled", True)
    async with factory() as db:
        repo = SearchRepository(db, user)
        rows, capped, *_ = await repo.recall(
            SearchRequest(q="cat"),
            SimpleNamespace(
                timezone="UTC", scoring_time=datetime.now(timezone.utc).isoformat()
            ),
            [1.0] + [0.0] * 1023,
            None,
        )
        assert len(rows) == 3 and not capped and not repo.approximate


@pytest.mark.asyncio
async def test_ranked_album_keyset_freezes_scores_and_embedding_once(
    infra, monkeypatch
):
    from app.services import search_engine as engine

    factory, _, user = infra
    monkeypatch.setattr(settings, "search_snapshot_max_candidates", 2)
    embed = AsyncMock(return_value=([1.0] + [0.0] * 1023, False))
    monkeypatch.setattr(engine, "get_query_embedding", embed)
    now = datetime.now(timezone.utc) - timedelta(days=1)
    ids = [
        await add_photo(
            factory, user, status="done", taken_at=now, embedding=[1.0] + [0.0] * 1023
        )
        for _ in range(7)
    ]
    request = SearchRequest(
        q="cat", retrieval_mode="album", result_mode="select", limit=2
    )
    async with factory() as db:
        service = engine.SearchService(db, user)
        page = await service.search(request)
        got = []
        while True:
            got.extend(x["id"] for x in page["items"])
            if not page["next_cursor"]:
                break
            page = await service.search(
                request.model_copy(update={"cursor": page["next_cursor"]})
            )
        assert got == sorted(ids) and page["total_matches"] == 7
    embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_corrupt_embedding_cache_and_visual_switch(infra, monkeypatch):
    from app.services import search as search_module, search_visual_verifier as visual
    from app.services.ai import _EMB_URL, _is_mock
    from app.services.search_budget import BudgetExhausted

    _, redis, user = infra
    budget = SearchBudget(redis, user, uuid4())
    await budget.create()
    provider = AsyncMock(return_value=[0.01] * 1024)
    monkeypatch.setattr(search_module, "embed_query", provider)
    with budget.activate():
        key = cache_key(
            "embedding",
            [_EMB_URL, settings.qwen_embedding_model, 1024, "query", "cat", _is_mock()],
        )
        await redis.set(key, "[1,2]", ex=30)
        value, hit = await search_module.get_query_embedding("cat")
        assert len(value) == 1024 and not hit
        assert (await search_module.get_query_embedding("cat"))[1]
        monkeypatch.setattr(settings, "search_visual_verify_enabled", False)
        with pytest.raises(BudgetExhausted, match="visual_disabled"):
            await visual.judge_visual_candidates("cat", [])
    provider.assert_awaited_once()
