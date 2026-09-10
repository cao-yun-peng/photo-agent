# ruff: noqa: F811
"""Persistent Redis cost caps and independent, bounded execution scopes.

These tests never contact model providers. Real Redis Lua scripts own counters and
expiration; only the module-local monotonic clock is advanced for elapsed time.
"""

import asyncio
import os
from uuid import uuid4

import pytest

from app.config import settings
from app.services import search_budget as budget_module
from app.services.search_budget import (
    BudgetExhausted,
    RequestTimeout,
    SearchBudget,
    model_call,
    record_provider_usage,
    search_execution,
)
from app.services.search_engine import SearchService
from app.services.search_store import SearchStore
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_search_optimization_integration import browse_request

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]


@pytest.mark.asyncio
async def test_v2_create_is_idempotent_and_cannot_refresh_expiry_or_cost_limits(
    infra, monkeypatch
):
    _, redis, user = infra
    monkeypatch.setattr(settings, "search_snapshot_ttl_seconds", 600)
    monkeypatch.setattr(settings, "search_max_model_calls", 3)
    budget = SearchBudget(redis, user, uuid4())
    expires_at = await budget.create()
    seconds, micros = await redis.time()
    assert 590 < expires_at - seconds - micros / 1_000_000 <= 600
    assert await redis.hget(budget.key, "schema_version") == "2"
    assert not await redis.hexists(budget.key, "deadline")
    with budget.activate():
        await budget.reserve("text")
    original = await redis.hgetall(budget.key)
    ttl = await redis.pttl(budget.key)

    # A second owner cannot turn another request into fresh money or a fresh lease.
    monkeypatch.setattr(settings, "search_max_model_calls", 100)
    monkeypatch.setattr(settings, "search_snapshot_ttl_seconds", 900)
    returned_expiry = await budget.create()
    assert returned_expiry == pytest.approx(expires_at, abs=0.001)
    assert await redis.hgetall(budget.key) == original
    assert 0 < await redis.pttl(budget.key) <= ttl
    assert (await budget.usage())["calls"] == 1
    assert (await budget.usage())["max_calls"] == 3


@pytest.mark.asyncio
async def test_nested_execution_and_budget_activation_inherit_original_deadline(
    infra, monkeypatch
):
    _, redis, user = infra
    budget = SearchBudget(redis, user, uuid4())
    await budget.create()
    clock = [100.0]
    monkeypatch.setattr(budget_module, "monotonic", lambda: clock[0])

    with search_execution(timeout_seconds=5) as outer:
        assert outer.deadline == 105
        with budget.activate():
            clock[0] = 102
            with search_execution(timeout_seconds=999) as nested:
                assert nested is outer and nested.deadline == 105
                assert nested.remaining() == 3
                assert 0 < await budget.reserve("text") <= 3
            clock[0] = 106
            with pytest.raises(RequestTimeout, match="request_timeout"):
                await budget.reserve("text")
    assert (await budget.usage())["calls"] == 1

    # A new top-level activation supplies a fresh execution scope, preserving costs.
    with budget.activate():
        assert await budget.reserve("text") > 0
    assert (await budget.usage())["calls"] == 2


@pytest.mark.asyncio
async def test_parallel_requests_do_not_extend_each_others_execution_deadline(
    infra, monkeypatch
):
    _, redis, user = infra
    budget_id = uuid4()
    short_budget = SearchBudget(redis, user, budget_id)
    long_budget = SearchBudget(redis, user, budget_id)
    await short_budget.create()
    clock = [100.0]
    monkeypatch.setattr(budget_module, "monotonic", lambda: clock[0])
    short_ready, long_ready, advance = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def short_request():
        with search_execution(timeout_seconds=1) as scope, short_budget.activate():
            short_ready.set()
            await advance.wait()
            assert scope.deadline == 101
            with pytest.raises(RequestTimeout, match="request_timeout"):
                await short_budget.reserve("text")

    async def long_request():
        with search_execution(timeout_seconds=20) as scope, long_budget.activate():
            long_ready.set()
            await advance.wait()
            assert scope.deadline == 120
            assert 0 < await long_budget.reserve("text") <= 18

    tasks = [asyncio.create_task(short_request()), asyncio.create_task(long_request())]
    try:
        await asyncio.wait_for(asyncio.gather(short_ready.wait(), long_ready.wait()), 2)
        clock[0] = 102
        advance.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert (await short_budget.usage())["calls"] == 1


@pytest.mark.asyncio
async def test_derived_plans_share_one_atomic_cost_cap_and_original_expiration(
    infra, monkeypatch
):
    factory, redis, user = infra
    monkeypatch.setattr(settings, "search_max_model_calls", 2)
    async with factory() as db:
        service = SearchService(db, user)
        parent = await service.create_plan(browse_request())
        derived = [
            await service.derive_plan(parent, {"status": None}) for _ in range(3)
        ]
    plans = [parent, *derived]
    assert len({plan.id for plan in plans}) == 4
    assert {plan.budget_id for plan in plans} == {parent.budget_id}
    assert {plan.expires_at for plan in plans} == {parent.expires_at}
    budgets = [SearchBudget(redis, user, plan.budget_id) for plan in plans]
    expiry = float(await redis.hget(budgets[0].key, "expires_at"))
    assert parent.expires_at == pytest.approx(expiry, abs=0.001)
    ttl = await redis.pttl(budgets[0].key)

    async def reserve(budget):
        with search_execution(), budget.activate():
            return await budget.reserve("text")

    results = await asyncio.gather(
        *(reserve(budgets[index % len(budgets)]) for index in range(12)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, float) for result in results) == 2
    assert sum(isinstance(result, BudgetExhausted) for result in results) == 10
    assert [(await budget.usage())["calls"] for budget in budgets] == [2] * 4
    assert float(await redis.hget(budgets[0].key, "expires_at")) == expiry
    assert 0 < await redis.pttl(budgets[0].key) <= ttl
    for plan in plans:
        stored = await SearchStore(redis).load(user, plan.id)
        assert stored["plan"]["budget_id"] == str(parent.budget_id)
        assert stored["plan"]["expires_at"] == parent.expires_at


@pytest.mark.asyncio
async def test_model_request_timeout_keeps_precharge_and_new_scope_can_continue(infra):
    _, redis, user = infra
    budget = SearchBudget(redis, user, uuid4())
    await budget.create()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def incomplete_provider():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with search_execution(timeout_seconds=0.2), budget.activate():
        with pytest.raises(RequestTimeout, match="request_timeout"):
            await model_call("text", incomplete_provider)
    assert entered.is_set() and cancelled.is_set()
    assert (await budget.usage())["calls"] == 1
    assert (await budget.usage())["units"] == 2

    async def complete_provider():
        return "completed"

    with search_execution(), budget.activate():
        assert await model_call("text", complete_provider) == "completed"
    assert (await budget.usage())["calls"] == 2
    assert (await budget.usage())["units"] == 4


@pytest.mark.asyncio
async def test_external_cancellation_also_keeps_model_precharge(infra):
    _, redis, user = infra
    budget = SearchBudget(redis, user, uuid4())
    await budget.create()
    entered = asyncio.Event()

    async def provider():
        entered.set()
        await asyncio.Event().wait()

    async def request():
        with search_execution(), budget.activate():
            await model_call("text", provider)

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await budget.usage())["calls"] == 1
    assert (await budget.usage())["stage:text"] == 1


@pytest.mark.asyncio
async def test_legacy_deadline_and_redis_expiration_are_not_migrated_or_resurrected(
    infra,
):
    _, redis, user = infra
    budget = SearchBudget(redis, user, uuid4())
    await budget.create()
    seconds, micros = await redis.time()
    legacy_deadline = seconds + micros / 1_000_000 + 30
    # A genuine v1 record has no version or v2 lifetime field.
    await redis.hdel(budget.key, "schema_version", "expires_at")
    await redis.hset(budget.key, "deadline", legacy_deadline)
    with budget.activate():
        assert 0 < await budget.reserve("text") <= 30
    before = await redis.hgetall(budget.key)
    ttl = await redis.pttl(budget.key)
    await budget.create()
    assert await redis.hgetall(budget.key) == before
    assert 0 < await redis.pttl(budget.key) <= ttl

    await redis.hset(budget.key, "deadline", 0)
    with search_execution(timeout_seconds=999), budget.activate():
        with pytest.raises(BudgetExhausted, match="deadline_exceeded"):
            await budget.reserve("text")
    assert (await budget.usage())["calls"] == 1

    await redis.pexpire(budget.key, 30)
    await asyncio.sleep(0.06)
    assert not await redis.exists(budget.key)
    with budget.activate():
        with pytest.raises(BudgetExhausted):
            await budget.reserve("text")
        await record_provider_usage({"total_tokens": 100})
    assert not await redis.exists(budget.key)
