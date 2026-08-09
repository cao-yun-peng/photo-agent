"""Phase 3 智能增强与前端集成测试（无需真实数据库/外部服务）."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.skill import Skill
from app.models.user_profile import UserProfile
from app.services.agent import (
    AgentConstraints,
    AgentState,
    PhotoAgent,
    ToolRegistry,
    ToolSpec,
    _generate_clarification,
)
from app.services.agent_tools import recommend_skills_for_agent
from app.services.recommend import (
    _freshness_score,
    _keyword_match_score,
    _popularity_score,
    recommend_skills,
)
from app.services.search import smart_album_fallback


# ------------------------------------------------------------------
# Skill 主动推荐
# ------------------------------------------------------------------
def test_freshness_score_now() -> None:
    now = datetime.now(timezone.utc)
    assert _freshness_score(now) == pytest.approx(1.0, rel=1e-3)


def test_freshness_score_half_life() -> None:
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=30)
    assert _freshness_score(past) == pytest.approx(0.5, rel=1e-3)


def test_popularity_score_log_smoothing() -> None:
    assert _popularity_score(0) == pytest.approx(0.0)
    assert _popularity_score(100) > _popularity_score(10)


def test_keyword_match_score_case_insensitive() -> None:
    skill = MagicMock()
    skill.name = "Anime Style"
    skill.description = "Convert photo to anime"
    skill.prompt_template = "anime"
    score = _keyword_match_score(skill, {"anime", "猫"}, {"anime": 0.8, "猫": 0.5})
    assert score > 0


@pytest.mark.asyncio
async def test_recommend_skills_ranking() -> None:
    """用户 Skill 偏好应显著提升对应 Skill 的排名。"""
    user_id = uuid4()
    skill_official = Skill(
        id=uuid4(),
        name="动漫风",
        description="把照片变成动漫风格",
        prompt_template="anime style",
        is_official=True,
        use_count=100,
        created_at=datetime.now(timezone.utc),
    )
    skill_user = Skill(
        id=uuid4(),
        name="老照片修复",
        description="修复老照片",
        prompt_template="restore old photo",
        is_official=False,
        use_count=5,
        created_at=datetime.now(timezone.utc),
    )

    profile = UserProfile(
        user_id=user_id,
        skill_affinity={str(skill_user.id): 0.9},
        tag_affinity={"猫": 0.8},
    )

    profile_result = MagicMock()
    profile_result.scalar_one_or_none.return_value = profile

    skills_result = MagicMock()
    scalar_result = MagicMock()
    scalar_result.all.return_value = [skill_official, skill_user]
    skills_result.scalars.return_value = scalar_result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[profile_result, skills_result])

    items = await recommend_skills(db, user_id)
    assert len(items) == 2
    assert items[0]["id"] == str(skill_user.id)
    assert items[0]["reason"]


@pytest.mark.asyncio
async def test_recommend_skills_for_agent_tool() -> None:
    """Tool 层应正确转换 photo_ids 并返回统一结构。"""
    user_id = uuid4()
    photo_id = uuid4()
    db = MagicMock()

    with patch(
        "app.services.agent_tools.recommend_skills",
        new=AsyncMock(return_value=[{"id": str(uuid4()), "name": "测试 Skill"}]),
    ):
        result = await recommend_skills_for_agent(
            user_id=user_id,
            db=db,
            photo_ids=[str(photo_id)],
            limit=3,
        )

    assert result["ok"] is True
    assert len(result["items"]) == 1
    assert "Skill" in result["hint"]


# ------------------------------------------------------------------
# 主动澄清
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generate_clarification_options() -> None:
    """对模糊查询应自动生成时间/地点/类型等澄清选项。"""
    result = await _generate_clarification("找照片")
    assert result["needs_clarification"] is True
    assert any("地点" in opt for opt in result["options"])
    assert any("时间" in opt for opt in result["options"])


@pytest.mark.asyncio
async def test_generate_clarification_with_place() -> None:
    """当查询已包含地点时，不应再询问地点。"""
    result = await _generate_clarification("去年在西湖拍的照片")
    options_text = " ".join(result["options"])
    assert "地点" not in options_text


@pytest.mark.asyncio
async def test_active_clarification_after_two_failed_searches() -> None:
    """search_photos 连续失败 2 次后应自动返回澄清。"""
    db = MagicMock()

    async def empty_search(**kwargs):
        return {"ok": True, "items": [], "hint": "empty"}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_photos",
            description="mock search",
            parameters={"type": "object", "properties": {}},
            fn=empty_search,
        )
    )

    agent = PhotoAgent(
        db=db,
        registry=registry,
        constraints=AgentConstraints(
            max_searches=3,
            max_clarifications=2,
            enable_browse_fallback=False,
        ),
    )
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="找照片",
    )

    result1 = await agent._execute_tool(
        state.user_id,
        "search_photos",
        json.dumps({"query": "找照片"}),
        state,
    )
    assert state.search_attempts == 1
    assert not result1.get("needs_clarification")

    result2 = await agent._execute_tool(
        state.user_id,
        "search_photos",
        json.dumps({"query": "找照片"}),
        state,
    )
    assert state.search_attempts == 2
    assert state.clarification_attempts == 1
    assert result2.get("needs_clarification") is True
    assert result2.get("options")


# ------------------------------------------------------------------
# 智能全量相册兜底
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_smart_album_fallback_sorts_by_recency() -> None:
    """无查询时，照片应按拍摄时间倒序排列。"""
    user_id = uuid4()
    now = datetime.now(timezone.utc)

    def make_photo(taken_at):
        photo = MagicMock()
        photo.id = uuid4()
        photo.user_id = user_id
        photo.embedding = None
        photo.taken_at = taken_at
        photo.ai_analysis = {}
        return photo

    photo_old = make_photo(now - timedelta(days=60))
    photo_new = make_photo(now)

    result = MagicMock()
    result.scalars.return_value.all.return_value = [photo_old, photo_new]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    with patch(
        "app.services.search.get_user_profile",
        new=AsyncMock(return_value=None),
    ):
        page, next_cursor = await smart_album_fallback(
            db, user_id, query=None, limit=10
        )

    assert len(page) == 2
    assert page[0][0].id == photo_new.id
    assert page[1][0].id == photo_old.id
    assert next_cursor is None


@pytest.mark.asyncio
async def test_smart_album_fallback_with_query() -> None:
    """有查询时，应计算语义分并返回 next_cursor。"""
    user_id = uuid4()

    def make_photo(embedding, taken_at):
        photo = MagicMock()
        photo.id = uuid4()
        photo.user_id = user_id
        photo.embedding = embedding
        photo.taken_at = taken_at
        photo.ai_analysis = {}
        return photo

    # 使用相同 embedding，仅通过时间区分
    emb = [1.0] + [0.0] * 1023
    photo = make_photo(emb, datetime.now(timezone.utc))

    result = MagicMock()
    result.scalars.return_value.all.return_value = [photo]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    with patch(
        "app.services.search.get_user_profile",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.search.get_query_embedding",
        new=AsyncMock(return_value=(emb, False)),
    ):
        page, _next_cursor = await smart_album_fallback(
            db, user_id, query="猫", limit=1
        )

    assert len(page) == 1
    assert page[0][1] > 0  # semantic score > 0


# ------------------------------------------------------------------
# SSE 事件队列
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_run_with_event_queue() -> None:
    """带 event_queue 时，事件应实时入队。"""
    db = MagicMock()
    queue: asyncio.Queue = asyncio.Queue()

    # mock LLM，让它直接返回 final_answer
    async def mock_llm(*args, **kwargs):
        return (
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "id": "call-final",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps({"message": "ok"}),
                        },
                    }
                ],
            },
            {"total_tokens": 10, "prompt_tokens": 5, "completion_tokens": 5},
        )

    with patch("app.services.agent._llm_decide", new=mock_llm):
        agent = PhotoAgent(
            db=db,
            constraints=AgentConstraints(enable_browse_fallback=False),
        )
        _state, events = await agent.run(
            user_id=uuid4(),
            query="hello",
            event_queue=queue,
        )

    assert len(events) >= 2
    assert events[-1]["type"] == "final"

    queued_events = []
    while not queue.empty():
        queued_events.append(queue.get_nowait())
    assert len(queued_events) == len(events)
