"""Offline request-clock and worker continuation contract tests."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import agent_execution as execution
from app.services import search_budget as budgets
from app.services.agent_state import AgentState
from app.services.circuit_breaker import CircuitBreaker
from app.workers import search_tasks as worker


def state():
    return AgentState(session_id=uuid4(), user_id=uuid4(), original_query="猫")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,followup,args,tool_timeout,expected",
    [
        ("search_photos", None, {}, 15, 30),
        ("search_photos", "more_search_results", {}, 15, 30),
        ("search_photos", None, {"complete_result_set": True}, 20, 45),
        ("fallback_search", None, {"feedback_level": 1}, 20, 30),
        ("browse_candidates", None, {}, 20, 30),
    ],
)
async def test_tool_scope_covers_nested_calls_and_reserves_cleanup(
    monkeypatch, tool, followup, args, tool_timeout, expected
):
    monkeypatch.setattr(settings, "search_total_timeout_seconds", 45)
    monkeypatch.setattr(settings, "task_cleanup_timeout_seconds", 5)
    monkeypatch.setattr(settings, "agent_search_turn_budget_seconds", 30)
    clock = [100.0]
    monkeypatch.setattr(budgets, "monotonic", lambda: clock[0])
    current_state = state()
    current_state.search_action = "continue" if followup else None

    async def tool_fn(**kwargs):
        assert budgets.execution_remaining() == expected
        with budgets.search_execution(999) as inherited:
            assert inherited.remaining() == expected
            clock[0] += 2
        # A fallback subcall cannot restore its own full time allowance.
        with budgets.search_execution(999):
            assert budgets.execution_remaining() == expected - 2
        return {"ok": True, "items": []}

    result, invoked = await execution._invoke_registered_tool(
        SimpleNamespace(constraints=SimpleNamespace(tool_timeout=tool_timeout)),
        SimpleNamespace(timeout=None, fn=tool_fn),
        tool,
        args,
        current_state,
    )
    assert invoked and result["ok"]
    assert budgets._execution.get() is None


@pytest.mark.asyncio
async def test_existing_scope_is_not_restarted_at_tool_dispatch(monkeypatch):
    monkeypatch.setattr(budgets, "monotonic", lambda: 100.0)

    async def tool_fn(**kwargs):
        assert budgets.execution_remaining() == 3
        return {"ok": True}

    with budgets.search_execution(3) as parent:
        result, invoked = await execution._invoke_registered_tool(
            SimpleNamespace(constraints=SimpleNamespace(tool_timeout=60)),
            SimpleNamespace(timeout=None, fn=tool_fn),
            "fallback_search",
            {},
            state(),
        )
        assert budgets._execution.get() is parent
    assert result["ok"] and invoked


@pytest.mark.asyncio
async def test_non_search_tool_has_no_search_scope():
    async def tool_fn(**kwargs):
        assert budgets._execution.get() is None
        return {"ok": True}

    assert (
        await execution._invoke_registered_tool(
            SimpleNamespace(constraints=SimpleNamespace(tool_timeout=60)),
            SimpleNamespace(timeout=None, fn=tool_fn),
            "get_photo_detail",
            {},
            state(),
        )
    )[1]


@pytest.mark.asyncio
async def test_request_timeout_retains_existing_continuation_state():
    async def tool_fn(**kwargs):
        raise budgets.RequestTimeout()

    current_state = state()
    current_state.active_search = {
        "plan_id": "saved-plan",
        "next_cursor": "saved-cursor",
    }
    current_state.search_action = "continue"
    result, invoked = await execution._invoke_registered_tool(
        SimpleNamespace(constraints=SimpleNamespace(tool_timeout=60)),
        SimpleNamespace(timeout=None, fn=tool_fn),
        "search_photos",
        {"plan_id": "saved-plan", "cursor": "saved-cursor"},
        current_state,
    )
    assert not invoked
    assert result["stop_reason"] == "request_timeout"
    assert result["search_pending"] and not result["search_exhausted"]
    assert result["next_cursor"] == "saved-cursor"
    assert current_state.active_search == {
        "plan_id": "saved-plan",
        "next_cursor": "saved-cursor",
    }


@pytest.mark.parametrize(
    "incomplete",
    [
        {"next_cursor": "next"},
        {"stop_reason": "request_timeout"},
        {"search_pending": True},
    ],
)
def test_agent_state_never_calls_a_resumable_result_exhausted(incomplete):
    current_state = state()
    current_state.workflow_state = "searching"
    current_state.search_action = "continue"
    current_state.active_search = {"plan_id": "saved-plan"}
    execution._apply_result_to_state(
        "search_photos",
        {"query": "猫"},
        {
            "ok": True,
            "items": [],
            "search_exhausted": True,
            "result_set_complete": True,
            **incomplete,
        },
        current_state,
        [],
        None,
        None,
    )
    assert current_state.active_search["exhausted"] is False


@pytest.fixture
def worker_environment(monkeypatch):
    user, session, generation = map(str, (uuid4(), uuid4(), uuid4()))
    scope = f"{user}:{session}:{generation}"
    env = SimpleNamespace(
        scope=scope, user=user, current_scope=scope, statuses=[], pushed=[], trace=[]
    )
    monkeypatch.setattr(settings, "search_total_timeout_seconds", 45)
    monkeypatch.setattr(settings, "agent_search_visual_budget_seconds", 30)
    monkeypatch.setattr(settings, "task_cleanup_timeout_seconds", 5)

    @asynccontextmanager
    async def database():
        yield SimpleNamespace()

    async def status(search_scope, value):
        if search_scope != env.current_scope:
            return False
        env.statuses.append(value)
        return True

    async def get_status(search_scope):
        return env.statuses[-1] if search_scope == env.current_scope else "missing"

    async def push(search_scope, items):
        if search_scope != env.current_scope:
            return 0
        env.pushed.extend(items)
        return len(items)

    async def trace(search_scope, value):
        if search_scope == env.current_scope:
            env.trace.append(value)
            return True
        return False

    monkeypatch.setattr(worker, "AsyncSessionLocal", database)
    monkeypatch.setattr(worker, "set_prefetch_status", status)
    monkeypatch.setattr(worker, "get_prefetch_status", get_status)
    monkeypatch.setattr(worker, "push_verified_candidates", push)
    monkeypatch.setattr(worker, "set_candidate_trace_context", trace)
    return env


async def run_worker(env):
    return await worker.prefetch_search_candidates(
        {},
        env.scope,
        env.user,
        "猫",
        [],
        {"plan_id": "unchanged-plan", "cursor": "saved-cursor"},
    )


@pytest.mark.asyncio
async def test_worker_gets_fresh_local_clock_and_keeps_plan(
    monkeypatch, worker_environment
):
    env = worker_environment
    monkeypatch.setattr(budgets, "monotonic", lambda: 100.0)

    async def search(self, request, **kwargs):
        assert kwargs == {"plan_id": "unchanged-plan", "prefetch": True}
        assert request.cursor == "saved-cursor"
        assert budgets._execution.get() is not parent
        assert budgets.execution_remaining() == 25
        return {"items": [], "search_exhausted": True}

    monkeypatch.setattr(worker.SearchService, "search", search)
    with budgets.search_execution(1) as parent:
        result = await run_worker(env)
        assert budgets._execution.get() is parent and parent.remaining() == 1
    assert result["ok"] and env.statuses == ["running", "exhausted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected",
    [
        ({"items": [], "next_cursor": "next"}, "resumable"),
        ({"items": [{"id": "verified"}], "next_cursor": "next"}, "resumable"),
        ({"items": [], "stop_reason": "request_timeout"}, "resumable"),
        ({"items": [], "stop_reason": "budget_exhausted"}, "failed"),
        ({"items": [{"id": "verified"}], "stop_reason": "budget_exhausted"}, "failed"),
        ({"items": [], "stop_reason": "deadline_exceeded"}, "failed"),
        (
            {"items": [], "stop_reason": "budget_exhausted", "next_cursor": "next"},
            "resumable",
        ),
        ({"items": []}, "resumable"),
        ({"items": [], "search_exhausted": True}, "exhausted"),
        ({"items": [{"id": "verified"}], "result_set_complete": True}, "ready"),
        ({"items": [], "search_exhausted": True, "next_cursor": "next"}, "resumable"),
    ],
)
async def test_worker_requires_explicit_completion_before_exhausted(
    monkeypatch, worker_environment, response, expected
):
    async def search(self, request, **kwargs):
        return response

    monkeypatch.setattr(worker.SearchService, "search", search)
    result = await run_worker(worker_environment)
    assert result["ok"]
    assert worker_environment.statuses == ["running", expected]
    assert result["prefetch_status"] == expected


@pytest.mark.asyncio
async def test_worker_timeout_exception_preserves_resumable_status(
    monkeypatch, worker_environment
):
    async def search(self, request, **kwargs):
        raise budgets.RequestTimeout()

    monkeypatch.setattr(worker.SearchService, "search", search)
    result = await run_worker(worker_environment)
    assert result == {
        "ok": False,
        "reason": "request_timeout",
        "prefetch_status": "resumable",
    }
    assert worker_environment.statuses == ["running", "resumable"]


@pytest.mark.asyncio
async def test_provider_timeout_is_not_mislabeled_as_own_clock(
    monkeypatch, worker_environment
):
    async def search(self, request, **kwargs):
        raise TimeoutError("provider timer")

    monkeypatch.setattr(worker.SearchService, "search", search)
    result = await run_worker(worker_environment)
    assert result == {"ok": False, "reason": "prefetch_failed"}
    assert worker_environment.statuses == ["running", "failed"]


@pytest.mark.asyncio
async def test_outer_worker_timeout_waits_for_checkpoint_then_marks_resumable(
    monkeypatch, worker_environment
):
    monkeypatch.setattr(settings, "agent_search_visual_budget_seconds", 0.03)
    monkeypatch.setattr(settings, "task_cleanup_timeout_seconds", 0.01)
    checkpointed = asyncio.Event()

    async def search(self, request, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            checkpointed.set()

    monkeypatch.setattr(worker.SearchService, "search", search)
    result = await run_worker(worker_environment)
    assert result["reason"] == "request_timeout"
    assert checkpointed.is_set()
    assert worker_environment.statuses[-1] == "resumable"


@pytest.mark.asyncio
async def test_worker_cancellation_is_re_raised_after_checkpoint(
    monkeypatch, worker_environment
):
    entered, checkpointed = asyncio.Event(), asyncio.Event()

    async def search(self, request, **kwargs):
        try:
            entered.set()
            await asyncio.sleep(10)
        finally:
            checkpointed.set()

    monkeypatch.setattr(worker.SearchService, "search", search)
    task = asyncio.create_task(run_worker(worker_environment))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert checkpointed.is_set()
    assert worker_environment.statuses == ["running", "resumable"]


@pytest.mark.asyncio
async def test_old_generation_cannot_publish_results_or_status(
    monkeypatch, worker_environment
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def search(self, request, **kwargs):
        entered.set()
        await release.wait()
        return {"items": [{"id": "old-photo"}], "search_exhausted": True}

    monkeypatch.setattr(worker.SearchService, "search", search)
    task = asyncio.create_task(run_worker(worker_environment))
    await entered.wait()
    worker_environment.current_scope = f"{worker_environment.user}:{uuid4()}:{uuid4()}"
    release.set()
    result = await task
    assert not result["ok"]
    assert worker_environment.pushed == [] and worker_environment.statuses == [
        "running"
    ]


@pytest.mark.asyncio
async def test_worker_does_not_overwrite_service_failure_as_success(
    monkeypatch, worker_environment
):
    async def search(self, request, **kwargs):
        return {"ok": False, "error_type": "request_timeout", "items": []}

    monkeypatch.setattr(worker.SearchService, "search", search)
    result = await run_worker(worker_environment)
    assert not result["ok"] and result["reason"] == "request_timeout"
    assert worker_environment.statuses == ["running", "resumable"]


@pytest.mark.asyncio
async def test_request_timeouts_never_count_as_provider_failures():
    breaker = CircuitBreaker("offline", failure_threshold=2)
    operation = AsyncMock(side_effect=budgets.RequestTimeout())
    for _ in range(5):
        with pytest.raises(budgets.RequestTimeout):
            await breaker.call(operation)
    assert operation.await_count == 5
    assert breaker.state == "closed" and breaker.failure_count == 0


@pytest.mark.asyncio
async def test_actual_provider_timeouts_still_trip_circuit_breaker():
    breaker = CircuitBreaker("offline", failure_threshold=2)
    operation = AsyncMock(side_effect=TimeoutError("provider timeout"))
    for _ in range(2):
        with pytest.raises(TimeoutError):
            await breaker.call(operation)
    assert breaker.state == "open" and breaker.failure_count == 2


@pytest.mark.asyncio
async def test_cancelled_half_open_probe_can_be_retried():
    breaker = CircuitBreaker("offline")
    breaker.state = "half_open"
    entered = asyncio.Event()

    async def operation():
        entered.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(breaker.call(operation))
    await entered.wait()
    assert breaker._half_open_probe_in_flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not breaker._half_open_probe_in_flight
    assert breaker.state == "half_open" and breaker.failure_count == 0
    assert await breaker.call(AsyncMock(return_value="recovered")) == "recovered"
    assert breaker.state == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [budgets.RequestTimeout(), budgets.BudgetExhausted()])
async def test_local_stop_releases_half_open_probe_without_counting_failure(stop):
    breaker = CircuitBreaker("offline")
    breaker.state = "half_open"
    with pytest.raises(type(stop)):
        await breaker.call(AsyncMock(side_effect=stop))
    assert breaker.state == "half_open" and breaker.failure_count == 0
    assert not breaker._half_open_probe_in_flight
    assert await breaker.call(AsyncMock(return_value="recovered")) == "recovered"


@pytest.mark.asyncio
async def test_cancelling_older_closed_call_does_not_release_another_probe():
    breaker = CircuitBreaker("offline")
    entered, probing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def old_call():
        entered.set()
        await asyncio.sleep(10)

    async def probe():
        probing.set()
        await release.wait()

    old_task = asyncio.create_task(breaker.call(old_call))
    await entered.wait()
    # Other failed calls can open the circuit while a prior call is still active.
    breaker.state = "half_open"
    probe_task = asyncio.create_task(breaker.call(probe))
    await probing.wait()
    old_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task
    assert breaker._half_open_probe_in_flight
    release.set()
    await probe_task
    assert breaker.state == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [True, False])
@pytest.mark.parametrize("coverage", [None, {"complete": False}])
async def test_pending_request_without_coverage_does_not_enqueue_index_repairs(
    monkeypatch, pending, coverage
):
    monkeypatch.setattr(settings, "agent_search_auto_repair_index", True)
    current_state = state()
    current_state.search_action = "continue"
    enqueue = AsyncMock(return_value=3)
    dependencies = execution.AgentExecutionDependencies(
        candidate_pool_key=lambda scope: scope,
        enqueue_index_repairs=enqueue,
    )
    result = {
        "ok": True,
        "items": [],
        "search_pending": pending,
        "index_coverage": coverage,
    }
    if pending:
        result["stop_reason"] = "request_timeout"
    await execution._run_search_maintenance(
        SimpleNamespace(db=object()),
        dependencies,
        current_state.user_id,
        "search_photos",
        {"query": "猫"},
        result,
        current_state,
    )
    if pending or coverage is None:
        enqueue.assert_not_awaited()
        assert "index_repair_queued" not in result
    else:
        enqueue.assert_awaited_once()
        assert result["index_repair_queued"] == 3


@pytest.mark.parametrize(
    "pending", [{"search_pending": True}, {"stop_reason": "request_timeout"}]
)
def test_empty_pending_tool_result_explains_continuation(pending):
    from app.services.agent_tools import _search_tool_result

    result = _search_tool_result({"items": [], **pending})
    assert result["hint"] == "本次搜索暂停，进度已保存，可以继续查找"
    assert "没有找到" not in result["hint"]
