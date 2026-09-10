"""Offline service-flow regressions: real verification and cursor code, no network."""

from contextlib import asynccontextmanager, nullcontext
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
import time

import pytest

from app.config import settings
from app.services import search_engine as engine, search_verification as verification
from app.services.search_contracts import QueryPlan, SearchRequest, request_fingerprint
from app.services.search_reranker import RerankDecision


class MemoryStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def load(self, user, plan_id):
        assert self.snapshot["plan"]["user_id"] == str(user)
        assert self.snapshot["plan"]["id"] == str(plan_id)
        return self.snapshot

    @asynccontextmanager
    async def mutation(self, user, plan_id):
        yield await self.load(user, plan_id)


class Budget:
    def activate(self):
        return nullcontext()

    async def remaining(self):
        return 45.0

    async def reserve(self, *args):
        return 45.0

    async def usage(self):
        return {}


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.setattr(settings, "search_browse_early_return", True)
    monkeypatch.setattr(settings, "search_rerank_enabled", True)
    monkeypatch.setattr(settings, "search_rerank_top_k", 5)
    monkeypatch.setattr(settings, "search_visual_verify_enabled", False)
    monkeypatch.setattr(engine, "get_redis", AsyncMock(return_value=object()))
    monkeypatch.setattr(engine, "current_timezone", lambda: "UTC")
    monkeypatch.setattr(engine, "SearchBudget", lambda *args: Budget())
    monkeypatch.setattr(engine, "sign_get_url", lambda key: f"/test/{key}")

    def make(
        matches=(),
        *,
        count=12,
        mode="browse",
        limit=5,
        verdict="contradiction",
        soft=False,
        complete=False,
    ):
        user, pid = uuid4(), uuid4()
        request = SearchRequest(
            q="cat",
            auto_parse=False,
            verify_constraints=False,
            result_mode=mode,
            limit=limit,
            complete_result_set=complete,
        )
        plan = QueryPlan(
            id=pid,
            budget_id=pid,
            user_id=user,
            raw_query="cat",
            effective_query="cat",
            timezone="UTC",
            local_date=date(2026, 9, 9),
            scoring_time="2026-09-09T00:00:00+00:00",
            request_json=request.model_dump_json(),
            fingerprint=request_fingerprint(request, "UTC"),
            verification="soft" if soft else "strict",
            allow_visual=False,
            expires_at=time.time() + 600,
        )
        photos = [
            SimpleNamespace(
                id=UUID(int=i + 1),
                hash=str(i),
                updated_at=None,
                taken_at=None,
                ai_analysis={},
                ai_description=f"candidate {i}",
                oss_key=f"{i}.jpg",
                thumb_key=None,
                status="done",
            )
            for i in range(count)
        ]
        snapshot = {
            "plan": plan.model_dump(mode="json"),
            "rows": [engine._row((p, 1.0, 0.0, 0.0, 1.0)) for p in photos],
            "accepted": [],
            "scan": 0,
            "terminal": None,
            "rerank": {},
            "meta": {
                "index_coverage": {"complete": True, "semantic_complete": True},
                "candidate_cap_reached": False,
                "scope_reliable": True,
                "semantic_facets_required": False,
                "similarity_threshold": None,
                "threshold_filtered_count": 0,
            },
        }
        store = MemoryStore(snapshot)
        monkeypatch.setattr(engine, "SearchStore", lambda redis: store)
        service = engine.SearchService(object(), user)
        service.create_plan = AsyncMock(return_value=plan)

        async def live(policy, plan, rows, excluded=()):
            by_id = {str(p.id): p for p in photos}
            return [
                (row, by_id[row["id"]])
                for row in rows
                if UUID(row["id"]) not in excluded
            ]

        service.repository.live = AsyncMock(side_effect=live)
        matching = {str(photos[i].id) for i in matches}
        calls = []

        async def judge(query, evidence):
            calls.append([e["photo_id"] for e in evidence])
            return [
                RerankDecision(
                    e["candidate_key"],
                    "match" if e["photo_id"] in matching else verdict,
                    1.0,
                    "fixture evidence",
                )
                for e in evidence
            ], {}

        monkeypatch.setattr(verification, "judge_candidate_evidence", judge)
        return service, request, snapshot, calls, photos

    return make


@pytest.mark.asyncio
async def test_browse_yields_verified_batch_and_continues_without_loss_or_duplicates(
    scenario,
):
    service, request, snapshot, calls, photos = scenario(matches=(0, 7, 11))
    first = await service.search(request)
    assert [p["id"] for p in first["items"]] == [str(photos[0].id)]
    assert first["stop_reason"] == "verified_batch_ready"
    assert first["next_cursor"] and not first["search_exhausted"]
    assert not first["result_set_complete"] and first["unverified_count"] == 7
    assert len(calls) == 1 and snapshot["terminal"] is None

    second = await service.search(
        request.model_copy(update={"cursor": first["next_cursor"]})
    )
    third = await service.search(
        request.model_copy(update={"cursor": second["next_cursor"]})
    )
    assert [p["id"] for page in (first, second, third) for p in page["items"]] == [
        str(photos[i].id) for i in (0, 7, 11)
    ]
    assert third["next_cursor"] is None and third["search_exhausted"]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_empty_first_batch_keeps_searching_until_verified_match(scenario):
    service, request, snapshot, calls, photos = scenario(matches=(6,))
    result = await service.search(request)
    assert [p["id"] for p in result["items"]] == [str(photos[6].id)]
    assert len(calls) == 2 and snapshot["scan"] == 10
    assert result["stop_reason"] == "verified_batch_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,exhausted", [("contradiction", True), ("uncertain", False)]
)
async def test_no_match_scans_candidates_and_never_promotes_uncertainty(
    scenario, verdict, exhausted
):
    service, request, _, calls, _ = scenario(verdict=verdict)
    result = await service.search(request)
    assert result["items"] == [] and len(calls) == 3
    assert result["search_exhausted"] is exhausted
    assert result["stop_reason"] == (
        "candidates_exhausted" if exhausted else "verification_incomplete"
    )


@pytest.mark.asyncio
async def test_saved_verified_remainder_returns_without_another_model_call(scenario):
    service, request, _, calls, photos = scenario(matches=(0, 1, 2, 7), limit=2)
    first = await service.search(request)
    assert len(first["items"]) == 2 and first["stop_reason"] == "page_full"
    second = await service.search(
        request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert [p["id"] for p in second["items"]] == [str(photos[2].id)]
    assert second["next_cursor"] and len(calls) == 1


@pytest.mark.asyncio
async def test_excluded_saved_match_does_not_cause_empty_early_return(scenario):
    service, request, _, calls, photos = scenario(matches=(0, 1, 7), limit=1)
    first = await service.search(request)
    second = await service.search(
        request.model_copy(
            update={
                "cursor": first["next_cursor"],
                "exclude_photo_ids": [photos[1].id],
            }
        )
    )
    assert [p["id"] for p in second["items"]] == [str(photos[7].id)]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_feature_rollback_preserves_fill_page_behavior(scenario, monkeypatch):
    service, request, _, calls, _ = scenario(matches=(0, 7, 11))
    monkeypatch.setattr(settings, "search_browse_early_return", False)
    result = await service.search(request)
    assert len(result["items"]) == 3 and len(calls) == 3
    assert result["stop_reason"] == "candidates_exhausted"


@pytest.mark.asyncio
async def test_prefetch_still_collects_multiple_batches(scenario):
    service, request, _, calls, _ = scenario(matches=(0, 7, 11))
    result = await service.search(request, prefetch=True)
    assert len(result["items"]) == 3 and len(calls) == 3
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_select_preserves_requested_page_size_with_verification(scenario):
    service, request, _, calls, _ = scenario(matches=range(10), mode="select", limit=8)
    result = await service.search(request)
    assert len(result["items"]) == 8 and len(calls) == 2
    assert result["next_cursor"] and result["stop_reason"] == "page_full"


@pytest.mark.asyncio
async def test_complete_selection_preserves_exhaustive_contract(scenario):
    service, request, _, calls, _ = scenario(
        matches=range(12), mode="select", limit=20, complete=True
    )
    result = await service.search(request)
    assert len(result["items"]) == 12 and len(calls) == 3
    assert result["next_cursor"] is None and result["result_set_complete"]


@pytest.mark.asyncio
async def test_old_soft_plan_requires_new_search(scenario):
    service, request, _, calls, _ = scenario(soft=True, verdict="uncertain")
    with pytest.raises(engine.SearchError, match="search_verification_policy_changed"):
        await service.search(request)
    assert not calls


@pytest.mark.asyncio
async def test_request_timeout_keeps_continuation_resumable(scenario, monkeypatch):
    service, request, _, calls, photos = scenario(matches=(0, 7))
    first = await service.search(request)
    monkeypatch.setattr(Budget, "remaining", AsyncMock(return_value=0.0))
    later = await service.search(
        request.model_copy(update={"cursor": first["next_cursor"]})
    )
    assert later["items"] == [] and len(calls) == 1
    assert later["stop_reason"] == "request_timeout"
    assert later["next_cursor"] and later["search_pending"]
    assert not later["search_exhausted"]
    assert first["items"][0]["id"] == str(photos[0].id)


@pytest.mark.asyncio
async def test_verifier_outage_is_not_successful_empty_answer(scenario, monkeypatch):
    service, request, _, _, _ = scenario()
    monkeypatch.setattr(
        verification,
        "judge_candidate_evidence",
        AsyncMock(side_effect=ConnectionError()),
    )
    result = await service.search(request)
    assert result["items"] == [] and result["stop_reason"] == "verification_unavailable"
    assert not result["search_exhausted"]


@pytest.mark.asyncio
async def test_select_excludes_nonmatches_and_uncertain(scenario):
    service, request, _, calls, photos = scenario(
        matches=(0, 7), mode="select", limit=8, verdict="uncertain"
    )
    result = await service.search(request)
    assert [p["id"] for p in result["items"]] == [str(photos[i].id) for i in (0, 7)]
    assert all(p["verification_status"] == "match" for p in result["items"])
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_old_unverified_selection_snapshot_cannot_be_reused(scenario):
    service, request, snapshot, calls, _ = scenario(mode="select")
    snapshot["plan"]["verification"] = "off"
    with pytest.raises(engine.SearchError, match="search_verification_policy_changed"):
        await service.search(request, plan_id=snapshot["plan"]["id"])
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["browse", "best", "select"])
@pytest.mark.parametrize("verify", [True, False])
async def test_new_semantic_plans_always_strict(monkeypatch, mode, verify):
    budget = SimpleNamespace(
        create=AsyncMock(return_value=time.time() + 600), activate=nullcontext
    )
    store = SimpleNamespace(create=AsyncMock())
    monkeypatch.setattr(engine, "get_redis", AsyncMock())
    monkeypatch.setattr(engine, "SearchBudget", lambda *args: budget)
    monkeypatch.setattr(engine, "SearchStore", lambda *args: store)
    monkeypatch.setattr(settings, "search_rerank_require_match", False)
    service = engine.SearchService(object(), uuid4())
    plan = await service.create_plan(
        SearchRequest(
            q="排球少年", auto_parse=False, result_mode=mode, verify_semantic=verify
        )
    )
    assert plan.verification == "strict"


@pytest.mark.asyncio
async def test_explicit_timeline_plan_can_skip_verification(monkeypatch):
    budget = SimpleNamespace(
        create=AsyncMock(return_value=time.time() + 600), activate=nullcontext
    )
    store = SimpleNamespace(create=AsyncMock())
    monkeypatch.setattr(engine, "get_redis", AsyncMock())
    monkeypatch.setattr(engine, "SearchBudget", lambda *args: budget)
    monkeypatch.setattr(engine, "SearchStore", lambda *args: store)
    plan = await engine.SearchService(object(), uuid4()).create_plan(
        SearchRequest(
            q=" ", auto_parse=False, retrieval_mode="timeline", result_mode="select"
        )
    )
    assert plan.verification == "off"


def test_browse_hint_never_claims_confirmed_matches():
    from app.services.agent_tools import _search_tool_result

    result = _search_tool_result({"items": [{"id": "test"}], "browse_scope": "all"})
    assert "未经" in result["hint"] and "并非已确认匹配" in result["hint"]


@pytest.mark.asyncio
async def test_fallback_with_dates_never_converts_to_unverified_browsing(
    monkeypatch, scenario
):
    service, request, snapshot, _, _ = scenario()
    request = request.model_copy(update={"from_date": date(2026, 1, 1)})
    parent = QueryPlan.model_validate(snapshot["plan"]).model_copy(
        update={"request_json": request.model_dump_json()}
    )
    service.create_plan = AsyncMock(return_value=parent)
    service.derive_plan = AsyncMock(return_value=parent)
    service.search = AsyncMock(
        return_value={"items": [], "stop_reason": "candidates_exhausted"}
    )
    monkeypatch.setattr(engine, "get_redis", AsyncMock())
    result = await service.fallback(parent.request(), allow_unfiltered=True)
    assert result["items"] == []
    assert service.search.await_count == 2
    service.derive_plan.assert_awaited_once_with(parent, {"status": None})
