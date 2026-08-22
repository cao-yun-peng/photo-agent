"""第一阶段 Turn Resolver 与普通搜索快路径回归测试。"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.agent import AgentState, PhotoAgent, ToolRegistry, ToolSpec
from app.services.turn_resolver import resolve_turn, resolve_turn_by_rule


def _search_registry(search_fn) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search_fn,
        )
    )
    return registry


def test_explicit_search_uses_rule_route_without_model() -> None:
    plan = resolve_turn_by_rule("我想要狗的照片")

    assert plan is not None
    assert plan.can_use_search_fast_path
    assert plan.intent == "photo_search"
    assert plan.relation == "new"
    assert plan.source == "rule"
    assert plan.model_calls == 0
    assert plan.search is not None
    assert plan.search.query == "狗的照片"


def test_explicit_search_replaces_previous_search() -> None:
    plan = resolve_turn_by_rule(
        "我想要金毛的照片",
        active_search={"resolved_query": "狗的照片"},
    )

    assert plan is not None
    assert plan.intent == "photo_search"
    assert plan.relation == "replace"
    assert plan.search is not None
    assert plan.search.query == "金毛的照片"


def test_complete_user_selection_uses_deterministic_search_plan() -> None:
    plan = resolve_turn_by_rule("把全部自拍给我，我自己选")

    assert plan is not None and plan.search is not None
    assert plan.can_use_search_fast_path
    assert plan.search.result_mode == "select"
    assert plan.search.complete_result_set is True
    assert plan.search.query == "自拍"


def test_numbered_user_selection_preserves_fifty_not_thirty() -> None:
    plan = resolve_turn_by_rule("给我50张自拍，我来选")

    assert plan is not None and plan.search is not None
    assert plan.can_use_search_fast_path
    assert plan.search.result_mode == "select"
    assert plan.search.limit == 50
    assert plan.search.complete_result_set is False


def test_edit_and_capability_questions_stay_in_full_agent() -> None:
    for query in (
        "把第一张修成漫画风格",
        "照片怎么上传",
        "这张照片里是谁",
    ):
        plan = resolve_turn_by_rule(query)
        assert plan is not None
        assert plan.intent == "complex_agent"
        assert not plan.can_use_search_fast_path


def test_vague_search_requests_one_meaningful_clarification() -> None:
    plan = resolve_turn_by_rule("找照片")

    assert plan is not None
    assert plan.needs_clarification
    assert "人物" in plan.clarification_question
    assert plan.clarification_options


def test_common_date_is_parsed_locally() -> None:
    plan = resolve_turn_by_rule("找去年海边的照片")

    assert plan is not None and plan.search is not None
    assert plan.search.from_date == date(date.today().year - 1, 1, 1)
    assert plan.search.to_date == date(date.today().year - 1, 12, 31)
    assert plan.search.place == "海边"


@pytest.mark.asyncio
async def test_contextual_short_query_merges_with_one_resolver_call() -> None:
    raw = {
        "intent": "photo_search",
        "relation": "refine",
        "query": "金毛狗的照片",
        "from_date": None,
        "to_date": None,
        "place": None,
        "confidence": 0.93,
    }
    with (
        patch("app.services.turn_resolver._is_mock_llm", return_value=False),
        patch(
            "app.services.turn_resolver._resolve_contextual_with_llm",
            new=AsyncMock(return_value=(raw, 42)),
        ) as resolver_llm,
    ):
        plan = await resolve_turn(
            "金毛的呢",
            active_search={"resolved_query": "狗的照片"},
        )

    resolver_llm.assert_awaited_once()
    assert plan.can_use_search_fast_path
    assert plan.relation == "refine"
    assert plan.search is not None
    assert plan.search.query == "金毛狗的照片"
    assert plan.model_calls == 1
    assert plan.model_tokens == 42


@pytest.mark.asyncio
async def test_simple_search_fast_path_skips_agent_and_parser_models() -> None:
    captured: dict = {}

    async def search(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "items": [{"id": "dog-1", "ai_description": "草地上的狗"}],
            "result_mode": "browse",
            "index_coverage": {"complete": True},
        }

    llm = AsyncMock()
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="我想要狗的照片",
    )
    with patch("app.services.agent._llm_decide", new=llm):
        updated, events = await PhotoAgent(
            db=MagicMock(),
            registry=_search_registry(search),
            system_prompt="test",
        ).run(state.user_id, "我想要狗的照片", initial_state=state)

    llm.assert_not_awaited()
    assert captured["query"] == "狗的照片"
    assert captured["auto_parse"] is False
    assert captured["verify_semantic"] is False
    assert captured["verify_constraints"] is False
    assert updated.active_search["resolved_query"] == "狗的照片"
    assert [event["type"] for event in events] == [
        "start",
        "route",
        "tool_call",
        "tool_result",
        "final",
    ]
    assert events[-1]["payload"]["fast_path"] is True


@pytest.mark.asyncio
async def test_dog_then_golden_retriever_replaces_result_state() -> None:
    calls: list[dict] = []

    async def search(**kwargs):
        calls.append(kwargs)
        photo_id = "dog-1" if len(calls) == 1 else "golden-1"
        return {
            "ok": True,
            "items": [{"id": photo_id, "ai_description": kwargs["query"]}],
            "result_mode": "browse",
            "index_coverage": {"complete": True},
        }

    llm = AsyncMock()
    agent = PhotoAgent(
        db=MagicMock(),
        registry=_search_registry(search),
        system_prompt="test",
    )
    user_id = uuid4()
    with patch("app.services.agent._llm_decide", new=llm):
        first, _ = await agent.run(user_id, "我想要狗的照片")
        second, events = await agent.run(
            user_id,
            "我想要金毛的照片",
            initial_state=first,
        )

    llm.assert_not_awaited()
    assert [call["query"] for call in calls] == ["狗的照片", "金毛的照片"]
    assert second.active_search["relation"] == "replace"
    assert second.active_search["shown_photo_ids"] == ["golden-1"]
    assert second.last_search_items[0]["id"] == "golden-1"
    assert events[1]["payload"]["relation"] == "replace"


@pytest.mark.asyncio
async def test_complete_selfie_request_returns_all_in_first_turn_without_agent_llm() -> None:
    captured: dict = {}
    items = [
        {"id": f"selfie-{index}", "ai_description": f"自拍 {index}"}
        for index in range(73)
    ]

    async def search(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "items": items,
            "total": 73,
            "total_matches": 73,
            "result_mode": "select",
            "result_set_complete": True,
            "index_coverage": {"complete": True},
        }

    llm = AsyncMock()
    user_id = uuid4()
    with patch("app.services.agent._llm_decide", new=llm):
        state, events = await PhotoAgent(
            db=MagicMock(),
            registry=_search_registry(search),
            system_prompt="test",
        ).run(user_id, "把全部自拍给我，我自己选")

    llm.assert_not_awaited()
    assert captured["query"] == "自拍"
    assert captured["result_mode"] == "select"
    assert captured["complete_result_set"] is True
    assert len(events[-2]["payload"]["result"]["items"]) == 73
    assert "完整加载 73 张" in events[-1]["payload"]["message"]
    assert len(state.last_search_items) == 30
    assert len(state.active_search["shown_photo_ids"]) == 73
