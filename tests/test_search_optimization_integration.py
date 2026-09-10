# ruff: noqa: F811
"""Browse early-return contracts with real SQL, Redis cursors and budget accounting.

Only the embedding and verifier provider boundaries use deterministic responses.
Each continuation opens a new database session, so progress must survive in Redis.
"""

import asyncio
import os
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import delete

from app.config import settings
from app.models.photo import Photo
from app.services import search_budget as budget_module
from app.services import search_engine as engine, search_verification as verification
from app.services.search_budget import SearchBudget, model_call, search_execution
from app.services.search_contracts import SearchRequest
from app.services.search_reranker import RerankDecision
from app.services.search_store import SearchStore, decode_cursor
from tests.test_batch1_integration import add_photo, infra  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]


@pytest.fixture
def fixed_models(monkeypatch):
    monkeypatch.setattr(settings, "search_browse_early_return", True)
    monkeypatch.setattr(settings, "search_rerank_enabled", True)
    monkeypatch.setattr(settings, "search_rerank_require_match", True)
    monkeypatch.setattr(settings, "search_rerank_top_k", 5)
    monkeypatch.setattr(settings, "search_visual_verify_enabled", False)
    monkeypatch.setattr(settings, "search_ann_enabled", False)
    embedding = AsyncMock(return_value=([0.01] * 1024, False))
    monkeypatch.setattr(engine, "get_query_embedding", embedding)
    monkeypatch.setattr(
        engine, "parse_query", AsyncMock(side_effect=AssertionError("parsing disabled"))
    )
    monkeypatch.setattr(
        verification,
        "judge_visual_candidates",
        AsyncMock(side_effect=AssertionError("visual verification disabled")),
    )

    def install(matches):
        matching = set(matches)
        calls = []

        async def judge(query, evidence):
            async def response():
                calls.append([entry["photo_id"] for entry in evidence])
                return [
                    RerankDecision(
                        entry["candidate_key"],
                        "match" if entry["photo_id"] in matching else "contradiction",
                        1.0,
                        "deterministic integration fixture",
                    )
                    for entry in evidence
                ], {}

            # The real Redis reservation must precede the synthetic provider response.
            return await model_call("text", response)

        monkeypatch.setattr(verification, "judge_candidate_evidence", judge)
        return calls, embedding

    return install


async def indexed_photos(factory, user, count=12):
    # Equal vectors and zero recency/interaction weights give a stable UUID ordering.
    return sorted(
        [
            await add_photo(
                factory,
                user,
                status="done",
                embedding=[0.01] * 1024,
                ai_description="A cat in an integration fixture.",
                ai_analysis={
                    "analysis_version": "v5",
                    "parse_quality": "ok",
                    "objects": ["cat"],
                },
                photo_type="photo",
                is_selfie=False,
                people_count=0,
            )
            for _ in range(count)
        ]
    )


def browse_request(limit=5):
    return SearchRequest(
        q="cat",
        auto_parse=False,
        verify_constraints=False,
        verified_only=True,
        include_index_coverage=True,
        result_mode="browse",
        limit=limit,
        min_semantic_score=0,
        w_semantic=1,
        w_recency=0,
        w_interaction=0,
    )


async def search_page(factory, user, request):
    async with factory() as db:
        return await engine.SearchService(db, user).search(request)


@pytest.mark.asyncio
async def test_early_batch_persists_across_sessions_without_lost_or_duplicate_matches(
    infra, fixed_models
):
    factory, redis, user = infra
    ids = await indexed_photos(factory, user)
    expected = [ids[index] for index in (0, 7, 11)]
    calls, embedding = fixed_models(expected)
    request = browse_request()

    first = await search_page(factory, user, request)
    assert [item["id"] for item in first["items"]] == expected[:1]
    assert first["stop_reason"] == "verified_batch_ready"
    assert first["next_cursor"] and not first["search_exhausted"]
    assert not first["result_set_complete"] and first["unverified_count"] == 7
    store = SearchStore(redis)
    snapshot = await store.load(user, first["search_id"])
    assert snapshot["scan"] == 5 and snapshot["terminal"] is None
    assert [entry["id"] for entry in snapshot["accepted"]] == expected[:1]
    assert snapshot["plan"]["budget_id"] == first["search_id"]
    assert decode_cursor(first["next_cursor"], user) == (first["search_id"], 1)
    budget = SearchBudget(redis, user, snapshot["plan"]["budget_id"])
    expires_at = await redis.hget(budget.key, "expires_at")
    assert expires_at and await redis.hget(budget.key, "schema_version") == "2"
    assert not await redis.hexists(budget.key, "deadline")
    assert first["search_usage"]["calls"] == 1
    assert first["search_usage"]["candidate"] == 5

    second = await search_page(
        factory, user, request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert second["stop_reason"] == "verified_batch_ready"
    assert second["search_id"] == first["search_id"]
    assert second["search_usage"]["calls"] == 2
    assert second["search_usage"]["candidate"] == 10
    third = await search_page(
        factory, user, request.model_copy(update={"cursor": second["next_cursor"]})
    )
    got = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert got == expected and len(got) == len(set(got))
    assert third["next_cursor"] is None and third["search_exhausted"]
    assert third["stop_reason"] == "candidates_exhausted"
    assert third["search_usage"]["calls"] == 3
    assert third["search_usage"]["candidate"] == 12
    assert third["search_usage"]["stage:text"] == 3
    assert calls == [ids[:5], ids[5:10], ids[10:]]
    assert await redis.hget(budget.key, "expires_at") == expires_at
    assert (await store.load(user, first["search_id"]))["scan"] == 12
    embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_saved_verified_remainder_needs_no_new_sql_recall_or_model_budget(
    infra, fixed_models
):
    factory, redis, user = infra
    ids = await indexed_photos(factory, user)
    calls, embedding = fixed_models([ids[index] for index in (0, 1, 2, 7)])
    request = browse_request(limit=2)
    first = await search_page(factory, user, request)
    assert [item["id"] for item in first["items"]] == ids[:2]
    assert first["stop_reason"] == "page_full"

    second = await search_page(
        factory, user, request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert [item["id"] for item in second["items"]] == ids[2:3]
    assert second["stop_reason"] == "verified_batch_ready" and second["next_cursor"]
    assert second["search_usage"] == first["search_usage"]
    assert second["search_usage"]["calls"] == 1
    assert second["search_usage"]["candidate"] == 5
    assert len(calls) == 1
    assert (await SearchStore(redis).load(user, first["search_id"]))["scan"] == 5
    embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_waiting_past_45_seconds_can_continue_without_extending_plan_ttl(
    infra, fixed_models, monkeypatch
):
    factory, redis, user = infra
    ids = await indexed_photos(factory, user)
    calls, embedding = fixed_models([ids[0], ids[7]])
    request = browse_request()
    first = await search_page(factory, user, request)
    budget = SearchBudget(redis, user, first["search_id"])
    store = SearchStore(redis)
    plan_key = f"search:v3:plan:{user}:{first['search_id']}"
    initial_expiry = float(await redis.hget(budget.key, "expires_at"))

    # Simulate a 60-second-old persisted plan without changing the Redis server clock
    # or asyncio's clock. Both Redis lifetimes and the local execution clock age.
    async with store.mutation(user, first["search_id"]) as snapshot:
        snapshot["plan"]["expires_at"] -= 60
        aged_plan_expiry = snapshot["plan"]["expires_at"]
    await redis.hset(budget.key, "expires_at", initial_expiry - 60)
    for key in (plan_key, budget.key):
        await redis.pexpire(key, (await redis.pttl(key)) - 60_000)
    before_ttls = [await redis.pttl(key) for key in (plan_key, budget.key)]
    monotonic_now = budget_module.monotonic()
    monkeypatch.setattr(budget_module, "monotonic", lambda: monotonic_now + 60)

    later = await search_page(
        factory, user, request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert [item["id"] for item in later["items"]] == ids[7:8]
    assert later["stop_reason"] == "verified_batch_ready"
    assert not later["search_exhausted"] and not later["result_set_complete"]
    assert later["next_cursor"] and later["unverified_count"] == 2
    assert later["search_usage"]["calls"] == 2
    assert later["search_usage"]["candidate"] == 10 and len(calls) == 2
    snapshot = await store.load(user, first["search_id"])
    assert snapshot["scan"] == 10 and snapshot["terminal"] is None
    assert snapshot["plan"]["expires_at"] == aged_plan_expiry
    assert float(await redis.hget(budget.key, "expires_at")) == initial_expiry - 60
    for key, before in zip((plan_key, budget.key), before_ttls):
        assert 0 < await redis.pttl(key) <= before
    embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_early_return_cursor_cannot_reset_real_redis_model_call_cap(
    infra, fixed_models, monkeypatch
):
    factory, redis, user = infra
    ids = await indexed_photos(factory, user)
    calls, embedding = fixed_models([ids[0], ids[7]])
    monkeypatch.setattr(settings, "search_max_model_calls", 1)
    request = browse_request()
    first = await search_page(factory, user, request)
    assert first["stop_reason"] == "verified_batch_ready"
    budget = SearchBudget(redis, user, first["search_id"])
    expires_at = await redis.hget(budget.key, "expires_at")

    later = await search_page(
        factory, user, request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert later["items"] == [] and later["stop_reason"] == "budget_exhausted"
    assert not later["search_exhausted"] and later["next_cursor"] is None
    assert later["search_id"] == first["search_id"]
    assert later["search_usage"]["calls"] == later["search_usage"]["max_calls"] == 1
    assert later["search_usage"]["stage:text"] == 1 and len(calls) == 1
    assert await redis.hget(budget.key, "expires_at") == expires_at
    snapshot = await SearchStore(redis).load(user, first["search_id"])
    assert snapshot["plan"]["budget_id"] == first["search_id"]
    assert snapshot["scan"] == 5 and snapshot["terminal"] == "budget_exhausted"
    # Exhausting new verification must not hide results already accepted by the plan.
    async with factory() as db:
        accepted = await engine.SearchService(db, user).search(
            request, plan_id=first["search_id"]
        )
    assert [item["id"] for item in accepted["items"]] == ids[:1]
    assert accepted["search_usage"]["calls"] == 1 and len(calls) == 1
    embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_timeout_keeps_cursor_and_retries_unfinished_batch_without_loss(
    infra, fixed_models, monkeypatch
):
    factory, redis, user = infra
    ids = await indexed_photos(factory, user)
    expected = [ids[index] for index in (0, 7, 11)]
    calls, embedding = fixed_models(expected)
    request = browse_request()
    first = await search_page(factory, user, request)
    store = SearchStore(redis)
    initial_snapshot = await store.load(user, first["search_id"])
    provider = verification.judge_candidate_evidence
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def interrupted_judge(query, evidence):
        async def response():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        return await model_call("text", response)

    monkeypatch.setattr(verification, "judge_candidate_evidence", interrupted_judge)
    with search_execution(timeout_seconds=1):
        paused = await search_page(
            factory, user, request.model_copy(update={"cursor": first["next_cursor"]})
        )
    assert entered.is_set() and cancelled.is_set()
    assert paused["items"] == [] and paused["stop_reason"] == "request_timeout"
    assert paused["next_cursor"] and not paused["search_exhausted"]
    assert not paused["result_set_complete"]
    snapshot = await store.load(user, first["search_id"])
    assert snapshot["terminal"] is None and snapshot["scan"] == 5
    assert snapshot["accepted"] == initial_snapshot["accepted"]
    assert paused["search_usage"]["calls"] == 2
    assert paused["search_usage"]["candidate"] == 10

    monkeypatch.setattr(verification, "judge_candidate_evidence", provider)
    resumed = await search_page(
        factory, user, request.model_copy(update={"cursor": paused["next_cursor"]})
    )
    final = await search_page(
        factory, user, request.model_copy(update={"cursor": resumed["next_cursor"]})
    )
    got = [item["id"] for page in (first, resumed, final) for item in page["items"]]
    assert got == expected and len(got) == len(set(got))
    assert final["next_cursor"] is None and final["search_exhausted"]
    # The timed-out provider attempt remains charged before the repeated batch.
    assert resumed["search_usage"]["calls"] == 3
    assert final["search_usage"]["calls"] == 4
    assert final["search_usage"]["candidate"] == 17
    assert calls == [ids[:5], ids[5:10], ids[10:]]
    embedding.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleted_saved_match_does_not_end_continuation_with_empty_early_batch(
    infra, fixed_models
):
    factory, redis, user = infra
    ids = await indexed_photos(factory, user)
    calls, embedding = fixed_models([ids[0], ids[1], ids[7]])
    request = browse_request(limit=1)
    first = await search_page(factory, user, request)
    assert [item["id"] for item in first["items"]] == ids[:1]
    async with factory() as db:
        await db.execute(delete(Photo).where(Photo.id == UUID(ids[1])))
        await db.commit()

    later = await search_page(
        factory, user, request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert [item["id"] for item in later["items"]] == ids[7:8]
    assert len(calls) == 2 and later["search_usage"]["calls"] == 2
    snapshot = await SearchStore(redis).load(user, first["search_id"])
    assert snapshot["scan"] == 10 and snapshot["terminal"] is None
    assert snapshot["invalidated"] == [ids[1]]
    assert decode_cursor(later["next_cursor"], user) == (first["search_id"], 3)
    embedding.assert_awaited_once()
