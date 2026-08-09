"""Phase 2 个性化相关单元测试（无需真实数据库/外部服务）."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.user_event import UserEvent
from app.services.agent import AgentState, ask_clarification
from app.services.events import EVENT_TYPES, log_event
from app.services.profile import (
    _PROFILE_HALF_LIFE_DAYS,
    _decay_weight,
    _normalize_scores,
)


# ------------------------------------------------------------------
# 行为事件写入
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_log_event_rejects_unknown_type() -> None:
    with pytest.raises(ValueError):
        await log_event(
            user_id=uuid4(),
            event_type="unknown_event",
            payload={"foo": 1},
        )


@pytest.mark.asyncio
async def test_log_event_with_external_db() -> None:
    """log_event 接受外部 db 时只 flush，不自行 commit。"""
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    event = await log_event(
        user_id=uuid4(),
        event_type="search_click",
        payload={"photo_id": str(uuid4()), "query": "猫"},
        db=db,
    )
    assert isinstance(event, UserEvent)
    assert event.event_type == "search_click"
    db.add.assert_called_once_with(event)
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_event_types_cover_phase2_requirements() -> None:
    assert "generation_complete" in EVENT_TYPES
    assert "search_click" in EVENT_TYPES
    assert "skill_browse" in EVENT_TYPES
    assert "photo_interact" in EVENT_TYPES


# ------------------------------------------------------------------
# 画像聚合：纯函数
# ------------------------------------------------------------------
def test_decay_weight_now() -> None:
    now = datetime.now(timezone.utc)
    assert _decay_weight(now, now) == pytest.approx(1.0)


def test_decay_weight_past() -> None:
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=_PROFILE_HALF_LIFE_DAYS)
    assert _decay_weight(past, now) == pytest.approx(0.5, rel=1e-3)


def test_decay_weight_future_treated_as_now() -> None:
    now = datetime.now(timezone.utc)
    future = now + timedelta(days=5)
    assert _decay_weight(future, now) == pytest.approx(1.0)


def test_normalize_scores() -> None:
    scores = {"a": 10.0, "b": 5.0, "c": 0.0}
    normalized = _normalize_scores(scores)
    assert normalized["a"] == 1.0
    assert normalized["b"] == 0.5
    assert normalized["c"] == 0.0


def test_normalize_scores_empty() -> None:
    assert _normalize_scores({}) == {}


# ------------------------------------------------------------------
# Agent 状态序列化与澄清工具
# ------------------------------------------------------------------
def test_agent_state_roundtrip() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="找一张海边照片",
    )
    state.search_attempts = 2
    state.fallback_level = 1
    state.rejected_photo_ids = {"a", "b"}

    data = state.to_json()
    restored = AgentState.from_json(data)

    assert restored.session_id == state.session_id
    assert restored.user_id == state.user_id
    assert restored.original_query == state.original_query
    assert restored.search_attempts == 2
    assert restored.fallback_level == 1
    assert restored.rejected_photo_ids == {"a", "b"}


@pytest.mark.asyncio
async def test_ask_clarification_tool() -> None:
    result = await ask_clarification(
        question="您想找哪种类型的照片？",
        options=["风景", "人物", "宠物"],
    )
    assert result["needs_clarification"] is True
    assert "风景" in result["options"]
