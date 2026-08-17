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
