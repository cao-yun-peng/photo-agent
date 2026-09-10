# ruff: noqa: F811
"""Offline boundary regressions for resumable search execution timeouts."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import settings
from app.services import query_parser as parser, search_budget as budget_module
from app.services import search_engine as engine, search_verification as verification
from app.services.search_budget import BudgetExhausted, RequestTimeout, search_execution
from app.services.search_contracts import QueryPlan, request_fingerprint
from app.services.search_reranker import RerankDecision
from app.services.search_store import decode_cursor
from tests.test_search_optimization import Budget, scenario  # noqa: F401


@pytest.mark.asyncio
async def test_initialization_timeout_retains_plan_and_cursor_until_initialization_resumes(
    scenario, monkeypatch
):
    service, request, snapshot, calls, photos = scenario(matches=(0, 7))
    rows, meta = snapshot["rows"], snapshot["meta"]
    snapshot.update(rows=None, meta={})
    initialize = AsyncMock(side_effect=RequestTimeout())
    monkeypatch.setattr(service, "_initialize", initialize)

    pending = await service.search(request)
    assert pending["items"] == [] and pending["stop_reason"] == "request_timeout"
    assert pending["search_pending"] and not pending["search_exhausted"]
    assert not pending["result_set_complete"]
    assert decode_cursor(pending["next_cursor"], service.user_id) == (
        pending["search_id"],
        0,
    )
    assert snapshot["rows"] is None and snapshot["terminal"] is None
    assert snapshot["scan"] == 0 and not snapshot["accepted"] and not calls

    async def restore(state, plan, budget):
        state.update(rows=rows, meta=meta)

    initialize.side_effect = restore
    resumed = await service.search(
        request.model_copy(update={"cursor": pending["next_cursor"]})
    )
    assert resumed["search_id"] == pending["search_id"]
    assert [item["id"] for item in resumed["items"]] == [str(photos[0].id)]
    assert not resumed["search_pending"]
    assert snapshot["scan"] == 5 and snapshot["terminal"] is None
    assert initialize.await_count == 2 and len(calls) == 1
    service.create_plan.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefetch", [False, True])
async def test_later_batch_timeout_keeps_completed_items_and_resumes_at_correct_offset(
    scenario, monkeypatch, prefetch
):
    service, request, snapshot, calls, photos = scenario(matches=(0, 1, 7, 11))
    monkeypatch.setattr(settings, "search_browse_early_return", False)
    judge = verification.judge_candidate_evidence
    attempts = []

    async def interrupt_second_batch(query, evidence):
        attempts.append([entry["photo_id"] for entry in evidence])
        if len(attempts) == 2:
            raise RequestTimeout()
        return await judge(query, evidence)

    monkeypatch.setattr(
        verification, "judge_candidate_evidence", interrupt_second_batch
    )
    paused = await service.search(request, prefetch=prefetch)
    assert [item["id"] for item in paused["items"]] == [
        str(photos[index].id) for index in (0, 1)
    ]
    assert paused["stop_reason"] == "request_timeout" and paused["search_pending"]
    assert decode_cursor(paused["next_cursor"], service.user_id) == (
        paused["search_id"],
        2,
    )
    assert snapshot["scan"] == 5 and snapshot["terminal"] is None
    assert len(snapshot["accepted"]) == 2
    assert snapshot["rerank"]["candidates_checked"] == 5

    resumed = await service.search(
        request.model_copy(update={"cursor": paused["next_cursor"]}), prefetch=prefetch
    )
    combined = [item["id"] for page in (paused, resumed) for item in page["items"]]
    assert combined == [str(photos[index].id) for index in (0, 1, 7, 11)]
    assert len(combined) == len(set(combined))
    assert attempts[1] == attempts[2]
    assert len(attempts) == 4 and len(calls) == 3
    assert resumed["next_cursor"] is None and resumed["search_exhausted"]
    assert snapshot["scan"] == 12 and snapshot["terminal"] is None
    assert snapshot["rerank"]["candidates_checked"] == 12


@pytest.mark.asyncio
async def test_visual_timeout_does_not_commit_uncertain_batch_and_can_retry(
    scenario, monkeypatch
):
    service, request, snapshot, calls, photos = scenario(count=3, verdict="uncertain")
    monkeypatch.setattr(settings, "search_visual_verify_enabled", True)
    monkeypatch.setattr(settings, "search_visual_verify_top_k", 3)
    snapshot["plan"]["allow_visual"] = True
    service.create_plan.return_value = QueryPlan.model_validate(snapshot["plan"])
    visual = AsyncMock(side_effect=RequestTimeout())
    monkeypatch.setattr(verification, "judge_visual_candidates", visual)

    paused = await service.search(request)
    assert paused["items"] == [] and paused["stop_reason"] == "request_timeout"
    assert paused["next_cursor"] and paused["search_pending"]
    assert snapshot["scan"] == 0 and snapshot["terminal"] is None
    assert snapshot["accepted"] == [] and snapshot["rerank"] == {}
    assert paused["unverified_count"] == 3
    visual.assert_awaited_once()

    async def verified(query, candidates):
        return [
            RerankDecision(
                item.candidate_key,
                "match" if item.photo_id == str(photos[0].id) else "contradiction",
                1.0,
                "fixture visual evidence",
            )
            for item in candidates
        ], {}

    visual.side_effect = verified
    resumed = await service.search(
        request.model_copy(update={"cursor": paused["next_cursor"]})
    )
    assert [item["id"] for item in resumed["items"]] == [str(photos[0].id)]
    assert resumed["items"][0]["verification_status"] == "match"
    assert resumed["next_cursor"] is None and resumed["search_exhausted"]
    assert snapshot["scan"] == 3 and snapshot["terminal"] is None
    assert snapshot["rerank"]["candidates_checked"] == 3
    assert snapshot["rerank"]["uncertain_count"] == 0
    assert len(calls) == 2 and visual.await_count == 2


@pytest.mark.asyncio
async def test_query_parser_propagates_request_timeout_without_rule_based_fallback(
    monkeypatch,
):
    monkeypatch.setattr(parser, "_is_mock", lambda: False)
    call = AsyncMock(side_effect=RequestTimeout())
    monkeypatch.setattr(parser, "agent_llm_breaker", SimpleNamespace(call=call))
    fallback = Mock(side_effect=AssertionError("timeout must not silently relax query"))
    monkeypatch.setattr(parser, "_rule_based_parse", fallback)

    with pytest.raises(RequestTimeout, match="request_timeout"):
        await parser.parse_query("昨天在公园拍的猫", timezone_name="UTC")
    call.assert_awaited_once()
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_nested_create_search_and_fallback_share_the_callers_execution_scope(
    scenario, monkeypatch
):
    service, request, snapshot, _, _ = scenario(count=0)
    template_meta = snapshot["meta"]
    store = engine.SearchStore(object())
    created = []
    clock = [100.0]
    monkeypatch.setattr(budget_module, "monotonic", lambda: clock[0])
    observed = []

    @contextmanager
    def observe_scope(*args, **kwargs):
        with search_execution(*args, **kwargs) as scope:
            observed.append(scope)
            yield scope

    monkeypatch.setattr(engine, "search_execution", observe_scope)
    monkeypatch.delattr(service, "create_plan")

    async def create_budget(self):
        clock[0] = 101
        return snapshot["plan"]["expires_at"]

    monkeypatch.setattr(Budget, "create", create_budget, raising=False)

    async def create_snapshot(value):
        created.append(value["plan"].copy())
        snapshot.clear()
        snapshot.update(value)

    monkeypatch.setattr(store, "create", create_snapshot, raising=False)
    initialization_count = 0

    async def initialize(state, plan, budget):
        nonlocal initialization_count
        initialization_count += 1
        clock[0] += 1 if initialization_count == 1 else 2
        budget_module.execution_remaining()
        state.update(rows=[], meta=template_meta.copy())

    monkeypatch.setattr(service, "_initialize", initialize)
    with search_execution(timeout_seconds=3) as caller:
        result = await service.fallback(request, allow_unfiltered=False)

    assert len(observed) >= 5 and all(scope is caller for scope in observed)
    assert all(scope.deadline == 103 for scope in observed)
    assert initialization_count == 2 and len(created) == 2
    assert created[0]["budget_id"] == created[1]["budget_id"]
    assert created[0]["expires_at"] == created[1]["expires_at"]
    assert result["stop_reason"] == "request_timeout" and result["search_pending"]
    assert result["fallback_level"] == 1 and result["next_cursor"]
    assert snapshot["terminal"] is None


@pytest.mark.asyncio
async def test_album_context_timeout_preserves_legacy_deadline_classification(
    scenario, monkeypatch
):
    service, request, snapshot, _, _ = scenario(mode="select", count=1)
    request = request.model_copy(update={"retrieval_mode": "album"})
    plan = QueryPlan.model_validate(snapshot["plan"]).model_copy(
        update={
            "request_json": request.model_dump_json(),
            "fingerprint": request_fingerprint(request, "UTC"),
        }
    )
    snapshot["plan"] = plan.model_dump(mode="json")
    service.create_plan.return_value = plan
    budget = Budget()
    budget.remaining = AsyncMock(
        side_effect=[0.01, BudgetExhausted("deadline_exceeded")]
    )
    monkeypatch.setattr(engine, "SearchBudget", lambda *args: budget)
    entered = asyncio.Event()

    async def embedding(query):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "get_query_embedding", embedding)
    result = await service.search(request)
    assert entered.is_set() and budget.remaining.await_count == 2
    assert result["stop_reason"] == "deadline_exceeded"
    assert not result["search_pending"] and not result["search_exhausted"]
    assert result["next_cursor"] is None
    assert "album_context" not in snapshot
