"""Execution clocks/cancellation use no network; Redis atomic scripts have integration coverage."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import search_budget as budgets, search_cache


def make_budget():
    # Redis only supplies a live plan deadline here. These tests exercise local
    # scopes and dispatch, rather than reproducing the Lua quota implementation.
    redis = AsyncMock()
    redis.eval.return_value = [1, 600_000]
    return budgets.SearchBudget(redis, uuid4(), uuid4()), redis


def test_nested_execution_reuses_deadline_and_restores_context(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(budgets, "monotonic", lambda: clock[0])
    with budgets.search_execution(45) as outer:
        clock[0] += 10
        with budgets.search_execution(1000) as inner:
            assert inner is outer and inner.remaining() == 35
        clock[0] += 35
        with pytest.raises(budgets.RequestTimeout):
            outer.remaining()
    assert budgets._execution.get() is None
    with budgets.search_execution(45) as next_request:
        assert next_request is not outer and next_request.remaining() == 45


def test_worker_fresh_scope_restores_parent(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(budgets, "monotonic", lambda: clock[0])
    with budgets.search_execution(12) as foreground:
        clock[0] += 5
        with budgets.search_execution(30, fresh=True) as worker:
            assert worker is not foreground and worker.remaining() == 30
            with budgets.search_execution(1000) as nested:
                assert nested is worker
        assert budgets._execution.get() is foreground
        assert foreground.remaining() == 7


@pytest.mark.asyncio
async def test_activate_after_user_idle_gets_new_execution_not_new_budget(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(budgets, "monotonic", lambda: clock[0])
    budget, redis = make_budget()
    allowance = budgets.settings.search_total_timeout_seconds
    with budget.activate():
        assert await budget.remaining() == allowance
        clock[0] += allowance + 1
        with pytest.raises(budgets.RequestTimeout):
            await budget.remaining()
    with budget.activate():
        assert await budget.remaining() == allowance
    assert budgets._active.get() is None
    # Continuation reads the same hash; it does not create/refill any quotas.
    assert {call.args[2] for call in redis.eval.await_args_list} == {budget.key}
    assert all("hincrby" not in call.args[0] for call in redis.eval.await_args_list)


@pytest.mark.asyncio
async def test_expired_execution_never_reserves_or_calls_provider():
    budget, redis = make_budget()
    provider = AsyncMock()
    with budgets.search_execution(0), budget.activate():
        with pytest.raises(budgets.RequestTimeout):
            await budgets.model_call("text", provider)
    redis.eval.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_timeout_retains_dispatch_reservation():
    budget, redis = make_budget()
    entered = asyncio.Event()

    async def provider():
        entered.set()
        await asyncio.Event().wait()

    with budgets.search_execution(0.03), budget.activate():
        with pytest.raises(budgets.RequestTimeout):
            await budgets.model_call("text", provider)
    assert entered.is_set()
    reservations = [
        call for call in redis.eval.await_args_list if "hincrby" in call.args[0]
    ]
    assert len(reservations) == 1
    assert reservations[0].args[3:] == ("text", 1, 2)
    assert all("hincrby" not in call.args[0] for call in redis.eval.await_args_list[1:])


@pytest.mark.asyncio
async def test_user_cancellation_does_not_refund_or_change_error_type():
    budget, redis = make_budget()
    entered = asyncio.Event()

    async def provider():
        entered.set()
        await asyncio.Event().wait()

    with budget.activate():
        task = asyncio.create_task(budgets.model_call("text", provider))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    redis.eval.assert_awaited_once()
    assert redis.eval.await_args.args[3:] == ("text", 1, 2)


@pytest.mark.asyncio
async def test_provider_timeout_is_not_mislabeled_as_request_timeout():
    budget, redis = make_budget()
    provider_timeout = TimeoutError("provider read timed out")
    with budget.activate():
        with pytest.raises(TimeoutError) as caught:
            await budgets.model_call("text", AsyncMock(side_effect=provider_timeout))
    assert caught.value is provider_timeout
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_deadline_is_terminal_even_in_fresh_execution():
    budget, redis = make_budget()
    redis.eval.return_value = [-2, 0]
    with budgets.search_execution(45, fresh=True), budget.activate():
        with pytest.raises(budgets.BudgetExhausted, match="deadline_exceeded"):
            await budget.remaining()
        with pytest.raises(budgets.BudgetExhausted, match="deadline_exceeded"):
            await budget.reserve("text")


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
async def test_invalid_reservation_cannot_refill_quota(amount):
    budget, redis = make_budget()
    with budget.activate(), pytest.raises(ValueError):
        await budget.reserve("text", amount)
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_hit_never_reserves_paid_work(monkeypatch):
    budget, redis = make_budget()
    redis.exists.return_value = False
    redis.get.return_value = '{"value": 42}'
    monkeypatch.setattr(search_cache, "get_redis", AsyncMock(return_value=redis))
    provider = AsyncMock(side_effect=AssertionError("cache hit called provider"))
    with budget.activate():
        result = await search_cache.cached_call(
            "cached-result", lambda: budgets.model_call("text", provider)
        )
    assert result == ({"value": 42}, True)
    provider.assert_not_awaited()
    assert not any("max_calls" in call.args[0] for call in redis.eval.await_args_list)


@pytest.mark.asyncio
async def test_cache_provider_timeout_before_compute_preserves_error(monkeypatch):
    budget, redis = make_budget()
    redis.exists.return_value = False
    redis.get.side_effect = [None, TimeoutError("Redis read timed out")]
    redis.set.return_value = True
    monkeypatch.setattr(search_cache, "get_redis", AsyncMock(return_value=redis))
    provider = AsyncMock()
    with budget.activate(), pytest.raises(TimeoutError, match="Redis read timed out"):
        await search_cache.cached_call("cache-timeout", provider)
    provider.assert_not_awaited()
