"""Agent 会话持久化与续接策略回归测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.services.agent import (
    AgentState,
    PhotoAgent,
    ToolRegistry,
    ToolSpec,
    _model_tool_content,
    _requests_complete_result_set,
    _requested_user_selection_limit,
)
from app.services.session import load_session


@pytest.mark.asyncio
async def test_load_session_accepts_active_and_completed_but_not_terminal_statuses() -> None:
    expected_session = object()
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = expected_session
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)

    session_id = uuid4()
    user_id = uuid4()
    loaded = await load_session(db, session_id, user_id)

    statement = db.execute.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    normalized = " ".join(sql.split())

    assert loaded is expected_session
    assert "agent_sessions.id =" in normalized
    assert "agent_sessions.user_id =" in normalized
    assert "agent_sessions.status IN ('active', 'completed')" in normalized
    assert "agent_sessions.expires_at >" in normalized
    assert "failed" not in normalized
    assert "abandoned" not in normalized


def test_agent_state_roundtrip_preserves_short_term_memory() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        recent_messages=[
            {"role": "user", "content": "搜索切尔西相关照片"},
            {"role": "assistant", "content": "找到了两张"},
        ],
        conversation_summary="用户正在查找足球照片",
        active_intent="search_photos",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
            "exhausted": False,
        },
    )

    restored = AgentState.from_json(state.to_json())

    assert restored.recent_messages == state.recent_messages
    assert restored.conversation_summary == state.conversation_summary
    assert restored.active_intent == "search_photos"
    assert restored.active_search == state.active_search


@pytest.mark.asyncio
async def test_more_followup_reuses_query_and_excludes_shown_photos() -> None:
    captured_search: dict = {}

    async def search(**kwargs):
        captured_search.update(kwargs)
        return {
            "ok": True,
            "items": [{"id": "photo-3", "ai_description": "切尔西球员"}],
            "next_cursor": None,
            "hint": "找到 1 张",
        }

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        recent_messages=[
            {"role": "user", "content": "搜索切尔西相关照片"},
            {"role": "assistant", "content": "找到了两张"},
        ],
        active_intent="search_photos",
        active_search={
            "raw_query": "搜索切尔西相关照片",
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
            "exhausted": False,
        },
        last_search_items=[
            {"id": "photo-1", "ai_description": "切尔西球员一"},
            {"id": "photo-2", "ai_description": "切尔西球员二"},
        ],
    )

    llm = AsyncMock()
    with patch("app.services.agent._llm_decide", new=llm):
        updated, events = await PhotoAgent(
            db=MagicMock(), registry=registry, system_prompt="test"
        ).run(state.user_id, "还有一张", initial_state=state)

    assert captured_search["query"] == "切尔西相关照片"
    assert captured_search["exclude_photo_ids"] == ["photo-1", "photo-2"]
    assert captured_search["limit"] == 1
    llm.assert_not_awaited()
    assert updated.active_search["shown_photo_ids"] == [
        "photo-1",
        "photo-2",
        "photo-3",
    ]
    assert updated.recent_messages[-2:] == [
        {"role": "user", "content": "还有一张"},
        {"role": "assistant", "content": "又找到 1 张符合条件的照片。"},
    ]
    tool_arguments = json.loads(events[1]["payload"]["arguments"])
    assert tool_arguments["query"] == "切尔西相关照片"
    assert tool_arguments["exclude_photo_ids"] == ["photo-1", "photo-2"]
    assert events[-1]["type"] == "final"


@pytest.mark.asyncio
async def test_more_followup_consumes_verified_candidate_pool_before_search() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        active_intent="search_photos",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
            "candidate_pool_items": [
                {"id": "photo-3", "ai_description": "第三张切尔西球员照片"}
            ],
            "candidate_pool_count": 1,
        },
    )
    search = AsyncMock()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )

    with patch("app.services.agent._llm_decide", new=AsyncMock()) as llm:
        updated, events = await PhotoAgent(
            db=MagicMock(), registry=registry, system_prompt="test"
        ).run(state.user_id, "还有一张", initial_state=state)

    search.assert_not_awaited()
    llm.assert_not_awaited()
    assert updated.last_search_items[0]["id"] == "photo-3"
    assert updated.active_search["candidate_pool_count"] == 0
    assert updated.active_search["shown_photo_ids"] == [
        "photo-1",
        "photo-2",
        "photo-3",
    ]
    assert events[2]["payload"]["result"]["source"] == "candidate_pool"


@pytest.mark.asyncio
async def test_initial_search_prefetches_verified_candidates_for_followup() -> None:
    calls: list[dict] = []

    async def search(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "items": [
                {"id": "photo-1", "ai_description": "切尔西第一张"},
                {"id": "photo-2", "ai_description": "切尔西第二张"},
            ],
            "rerank_check": {"applied": True, "degraded": False},
            "index_coverage": {"complete": True},
        }

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    state = AgentState(
        session_id=uuid4(), user_id=uuid4(), original_query="切尔西相关照片"
    )
    agent = PhotoAgent(db=MagicMock(), registry=registry, system_prompt="test")

    enqueue = AsyncMock(return_value=True)
    with patch(
        "app.workers.search_tasks.enqueue_search_prefetch", new=enqueue
    ):
        result = await agent._execute_tool(
            state.user_id,
            "search_photos",
            json.dumps({"query": "切尔西相关照片"}, ensure_ascii=False),
            state,
        )

    assert result["items"][0]["id"] == "photo-1"
    assert len(calls) == 1
    enqueue.assert_awaited_once_with(
        session_id=str(state.session_id),
        user_id=str(state.user_id),
        query="切尔西相关照片",
        exclude_photo_ids=["photo-1", "photo-2"],
    )
    assert state.active_search["candidate_pool_items"] == []
    assert state.active_search["prefetch_status"] == "queued"
    assert state.active_search["pool_key"].endswith(str(state.session_id))


@pytest.mark.asyncio
async def test_more_followup_consumes_background_verified_candidate() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        active_intent="search_photos",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
            "pool_key": "agent:search-pool:test",
            "prefetch_status": "queued",
        },
    )
    search = AsyncMock()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    candidate = {"id": "photo-3", "ai_description": "切尔西第三张"}

    with (
        patch(
            "app.services.agent.get_prefetch_status",
            new=AsyncMock(side_effect=["running", "ready"]),
        ),
        patch(
            "app.services.agent.wait_for_verified_candidate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "app.services.agent.candidate_pool_size",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "app.services.agent.set_prefetch_status", new=AsyncMock()
        ) as set_status,
    ):
        updated, events = await PhotoAgent(
            db=MagicMock(), registry=registry, system_prompt="test"
        ).run(state.user_id, "还有一张", initial_state=state)

    search.assert_not_awaited()
    set_status.assert_awaited_once_with(state.session_id, "exhausted")
    assert updated.last_search_items[0]["id"] == "photo-3"
    assert updated.active_search["shown_photo_ids"][-1] == "photo-3"
    assert events[2]["payload"]["result"]["source"] == "redis_candidate_pool"


@pytest.mark.asyncio
async def test_more_followup_reports_pending_without_running_foreground_search() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        active_intent="search_photos",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
            "pool_key": "agent:search-pool:test",
            "prefetch_status": "queued",
        },
    )
    search = AsyncMock()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )

    with (
        patch(
            "app.services.agent.get_prefetch_status",
            new=AsyncMock(side_effect=["running", "running"]),
        ),
        patch(
            "app.services.agent.wait_for_verified_candidate",
            new=AsyncMock(return_value=None),
        ),
    ):
        updated, events = await PhotoAgent(
            db=MagicMock(), registry=registry, system_prompt="test"
        ).run(state.user_id, "还有一张", initial_state=state)

    search.assert_not_awaited()
    assert updated.active_search["exhausted"] is False
    assert events[-2]["payload"]["result"]["search_pending"] is True
    assert "搜索进度" not in events[-1]["payload"]["message"]
    assert "还在继续筛选" in events[-1]["payload"]["message"]


@pytest.mark.asyncio
async def test_more_followup_timeout_is_resumable_not_generic_error() -> None:
    async def slow_search(**_kwargs):
        import asyncio

        await asyncio.sleep(0.1)
        return {"ok": True, "items": []}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=slow_search,
            timeout=0.01,
        )
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
        },
    )

    updated, events = await PhotoAgent(
        db=MagicMock(), registry=registry, system_prompt="test"
    ).run(state.user_id, "还有一张", initial_state=state)

    assert updated.active_search["exhausted"] is False
    assert events[-2]["payload"]["result"]["error_type"] == "timeout_resumable"
    assert "进度已经保留" in events[-1]["payload"]["message"]
    assert "出现问题" not in events[-1]["payload"]["message"]


@pytest.mark.asyncio
async def test_incomplete_index_does_not_claim_search_is_exhausted() -> None:
    async def search(**_kwargs):
        return {
            "ok": True,
            "items": [],
            "index_coverage": {
                "total_photos": 3,
                "indexed_photos": 2,
                "retrying_photos": 0,
                "unavailable_photos": 1,
                "complete": False,
            },
        }

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="切尔西相关照片",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
        },
    )

    with patch(
        "app.services.agent.enqueue_index_repairs", new=AsyncMock(return_value=1)
    ):
        updated, events = await PhotoAgent(
            db=MagicMock(), registry=registry, system_prompt="test"
        ).run(state.user_id, "还有一张", initial_state=state)

    assert updated.active_search["exhausted"] is False
    assert events[-2]["payload"]["result"]["index_repair_queued"] == 1
    assert "索引尚未完整" in events[-1]["payload"]["message"]


@pytest.mark.asyncio
async def test_nontrivial_followup_receives_recent_dialogue_and_structured_memory() -> None:
    captured: list[dict] = []

    async def decide(messages, _tools):
        captured.extend(json.loads(json.dumps(messages, ensure_ascii=False)))
        return (
            {
                "role": "assistant",
                "content": "需要查看第二张详情",
                "tool_calls": [
                    {
                        "id": "final",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps(
                                {"message": "已理解第二张照片"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            },
            {"total_tokens": 10},
        )

    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="搜索切尔西相关照片",
        recent_messages=[
            {"role": "user", "content": "搜索切尔西相关照片"},
            {"role": "assistant", "content": "找到了两张"},
        ],
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
        },
        last_search_items=[
            {"id": "photo-1", "ai_description": "第一张"},
            {"id": "photo-2", "ai_description": "第二张"},
        ],
    )

    with patch("app.services.agent._llm_decide", new=decide):
        await PhotoAgent(db=MagicMock(), system_prompt="test").run(
            state.user_id,
            "第二张是谁？",
            initial_state=state,
        )

    encoded = json.dumps(captured, ensure_ascii=False)
    assert "搜索切尔西相关照片" in encoded
    assert "切尔西相关照片" in encoded
    assert "photo-2" in encoded
    assert "第二张是谁" in encoded


@pytest.mark.asyncio
async def test_more_followup_cannot_clarify_or_browse_whole_album() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="还有一张",
        active_search={
            "resolved_query": "切尔西相关照片",
            "shown_photo_ids": ["photo-1", "photo-2"],
        },
        followup_type="more_search_results",
    )
    agent = PhotoAgent(db=MagicMock(), system_prompt="test")

    clarification = await agent._execute_tool(
        state.user_id,
        "ask_clarification",
        json.dumps({"question": "你想找什么？"}, ensure_ascii=False),
        state,
    )
    browse = await agent._execute_tool(
        state.user_id,
        "browse_candidates",
        "{}",
        state,
    )

    assert clarification["ok"] is False
    assert "不要澄清" in clarification["hint"]
    assert browse["ok"] is False
    assert "不能退化" in browse["hint"]


def _search_then_decision_sequence(*followups):
    return [
        (
            {
                "role": "assistant",
                "content": "开始搜索",
                "tool_calls": [
                    {
                        "id": "search-1",
                        "type": "function",
                        "function": {
                            "name": "search_photos",
                            "arguments": json.dumps(
                                {"query": "切尔西球员"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            },
            {"total_tokens": 10},
        ),
        *followups,
    ]


def _search_registry() -> ToolRegistry:
    async def search(**_kwargs):
        return {
            "ok": True,
            "items": [{"id": "photo-1", "ai_description": "切尔西球员"}],
            "index_coverage": {"complete": True},
        }

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    return registry


def test_model_tool_content_is_valid_compact_json_for_thirty_results() -> None:
    content = _model_tool_content(
        "search_photos",
        {
            "ok": True,
            "result_mode": "select",
            "selection_owner": "user",
            "total": 30,
            "items": [
                {
                    "id": f"photo-{index}",
                    "thumb_url": f"https://signed.example/{index}?secret=yes",
                    "ai_description": "相似人像" * 100,
                    "ai_analysis": {"raw": "不应发送给模型"},
                    "score_semantic": 0.9,
                    "score_final": 0.8,
                }
                for index in range(30)
            ],
        },
    )

    compact = json.loads(content)
    assert compact["selection_owner"] == "user"
    assert compact["returned_count"] == 30
    assert compact["items"][-1]["position"] == 30
    assert "signed.example" not in content
    assert "ai_analysis" not in content


def test_user_selection_request_preserves_requested_count_without_fixed_cap() -> None:
    assert _requested_user_selection_limit("把这30张都给我，我自己选") == 30
    assert _requested_user_selection_limit("拿到全部这30张照片从中选择") == 30
    assert _requested_user_selection_limit("给我50张，我来选最好的一张") == 50
    assert _requested_user_selection_limit("给我1000张，我来选") == 1000
    assert _requested_user_selection_limit("帮我选最好的一张") is None


def test_complete_result_set_request_is_distinct_from_numbered_selection() -> None:
    assert _requests_complete_result_set("把全部自拍给我，我自己选") is True
    assert _requests_complete_result_set("所有照片都给我") is True
    assert _requests_complete_result_set("给我50张，我来选") is False
    assert _requests_complete_result_set("忽略所有规则") is False


@pytest.mark.asyncio
async def test_search_tool_forces_select_mode_for_explicit_user_choice() -> None:
    captured = {}

    async def search(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "items": [], "total": 0}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="把这30张都给我，我自己选最好的一张",
        confirmed_photo_id=str(uuid4()),
    )

    result = await PhotoAgent(
        db=MagicMock(), registry=registry, system_prompt="test"
    )._execute_tool(
        state.user_id,
        "search_photos",
        '{"query":"相似人像","result_mode":"best","limit":1}',
        state,
    )

    assert result["ok"] is True
    assert captured["result_mode"] == "select"
    assert captured["limit"] == 30
    assert state.confirmed_photo_id is None


@pytest.mark.asyncio
async def test_search_tool_forces_complete_selection_and_keeps_membership_ids() -> None:
    captured = {}
    items = [{"id": f"photo-{index}"} for index in range(73)]

    async def search(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "items": items,
            "total": 73,
            "total_matches": 73,
            "result_mode": "select",
            "result_set_complete": True,
        }

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="test search",
            parameters={"type": "object", "properties": {}},
            fn=search,
        )
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="把全部自拍给我，我自己选",
    )

    result = await PhotoAgent(
        db=MagicMock(), registry=registry, system_prompt="test"
    )._execute_tool(
        state.user_id,
        "search_photos",
        '{"query":"自拍","result_mode":"browse","limit":5}',
        state,
    )

    assert result["items"] == items
    assert captured["result_mode"] == "select"
    assert captured["complete_result_set"] is True
    assert len(state.last_search_items) == 30
    assert len(state.active_search["shown_photo_ids"]) == 73
    assert state.active_search["filters"]["complete_result_set"] is True
    assert state.active_search["exhausted"] is True


@pytest.mark.asyncio
async def test_select_mode_cannot_apply_skill_before_user_confirmation() -> None:
    called = False

    async def apply(**_kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="apply_skill",
            description="test apply",
            parameters={"type": "object", "properties": {}},
            fn=apply,
        )
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="我自己选",
        active_search={"filters": {"result_mode": "select"}},
        last_search_items=[{"id": str(uuid4())}],
    )

    result = await PhotoAgent(
        db=MagicMock(), registry=registry, system_prompt="test"
    )._execute_tool(
        state.user_id,
        "apply_skill",
        json.dumps({"photo_id": state.last_search_items[0]["id"]}),
        state,
    )

    assert result["error_type"] == "confirmation_required"
    assert called is False


@pytest.mark.asyncio
async def test_function_calling_history_keeps_assistant_before_tool_result() -> None:
    llm = AsyncMock(
        side_effect=[
            (
                {
                    "role": "assistant",
                    "content": "开始搜索",
                    "tool_calls": [
                        {
                            "id": "search-1",
                            "type": "function",
                            "function": {
                                "name": "search_photos",
                                "arguments": '{"query":"切尔西球员"}',
                            },
                        }
                    ],
                },
                {"total_tokens": 10},
            ),
            (
                {"role": "assistant", "content": "已展示结果。", "tool_calls": []},
                {"total_tokens": 5},
            ),
        ]
    )
    state = AgentState(
        session_id=uuid4(), user_id=uuid4(), original_query="切尔西球员"
    )

    with patch("app.services.agent._llm_decide", new=llm):
        await PhotoAgent(
            db=MagicMock(), registry=_search_registry(), system_prompt="test"
        ).run(state.user_id, "切尔西球员", initial_state=state)

    second_messages = llm.await_args_list[1].args[0]
    assistant_index = next(
        index
        for index, message in enumerate(second_messages)
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    tool_message = second_messages[assistant_index + 1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "search-1"
    json.loads(tool_message["content"])


@pytest.mark.asyncio
async def test_post_search_llm_timeout_retries_once_and_uses_retry_result() -> None:
    llm = AsyncMock(
        side_effect=_search_then_decision_sequence(
            httpx.ConnectTimeout("first timeout"),
            (
                {
                    "role": "assistant",
                    "content": "已找到一张切尔西球员照片。",
                    "tool_calls": [],
                },
                {"total_tokens": 6},
            ),
        )
    )
    state = AgentState(
        session_id=uuid4(), user_id=uuid4(), original_query="切尔西球员"
    )

    with patch("app.services.agent._llm_decide", new=llm):
        _updated, events = await PhotoAgent(
            db=MagicMock(), registry=_search_registry(), system_prompt="test"
        ).run(state.user_id, "切尔西球员", initial_state=state)

    assert llm.await_count == 3
    assert events[-1]["type"] == "final"
    assert events[-1]["payload"]["message"] == "已找到一张切尔西球员照片。"
    assert not any(event["type"] == "error" for event in events)


@pytest.mark.asyncio
async def test_post_search_llm_retry_failure_uses_local_success_summary() -> None:
    llm = AsyncMock(
        side_effect=_search_then_decision_sequence(
            httpx.ConnectTimeout("first timeout"),
            httpx.ConnectTimeout("retry timeout"),
        )
    )
    state = AgentState(
        session_id=uuid4(), user_id=uuid4(), original_query="切尔西球员"
    )

    with patch("app.services.agent._llm_decide", new=llm):
        updated, events = await PhotoAgent(
            db=MagicMock(), registry=_search_registry(), system_prompt="test"
        ).run(state.user_id, "切尔西球员", initial_state=state)

    assert llm.await_count == 3
    assert updated.last_search_items[0]["id"] == "photo-1"
    assert events[-1]["type"] == "final"
    assert events[-1]["payload"]["partial_success"] is True
    assert events[-1]["payload"]["fallback"] == "local_search_summary"
    assert "已找到 1 张符合条件的照片" in events[-1]["payload"]["message"]
    assert "决策服务暂时不可用" not in events[-1]["payload"]["message"]
    assert not any(event["type"] == "error" for event in events)
