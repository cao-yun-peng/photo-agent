"""Agent 会话持久化与续接策略回归测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services.agent import AgentState, PhotoAgent, ToolRegistry, ToolSpec
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
