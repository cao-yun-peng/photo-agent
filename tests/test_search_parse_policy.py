"""自动查询解析的安全合并策略回归测试。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.photo import ParsedQuery, SearchQuery
from app.services.query_parser import (
    resolve_auto_parsed_query,
    should_apply_parsed_date_filters,
)


def test_visual_date_is_kept_in_semantic_query() -> None:
    parsed = ParsedQuery(
        semantic="台历",
        from_date=date(2026, 8, 15),
        to_date=date(2026, 8, 15),
        tags=[],
    )
    effective, from_date, to_date = resolve_auto_parsed_query(
        "2026年八月十五日的台历",
        parsed,
        from_date=None,
        to_date=None,
    )
    assert effective == "2026年八月十五日的台历"
    assert from_date is None
    assert to_date is None
    assert not should_apply_parsed_date_filters("写着9:41的手机锁屏")


def test_capture_time_is_applied_and_place_is_kept_for_embedding() -> None:
    parsed = ParsedQuery(
        semantic="雨天照片",
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 31),
        place="西湖",
        tags=["小橘"],
    )
    effective, from_date, to_date = resolve_auto_parsed_query(
        "找上个月在西湖拍的小橘雨天照片",
        parsed,
        from_date=None,
        to_date=None,
    )
    assert effective == "雨天照片 西湖 小橘"
    assert from_date == date(2026, 7, 1)
    assert to_date == date(2026, 7, 31)


@pytest.mark.asyncio
async def test_api_auto_tags_do_not_become_hard_photo_tags(monkeypatch) -> None:
    import app.api.search as search_api

    captured: dict[str, object] = {}

    async def fake_parse(_: str) -> ParsedQuery:
        return ParsedQuery(
            semantic="门垫",
            place=None,
            tags=["WELCOME", "门垫"],
        )

    async def fake_embedding(text: str) -> tuple[list[float], bool]:
        captured["effective_query"] = text
        return [0.0] * 1024, False

    async def fake_profile(*_args):
        return None

    class EmptyResult:
        def all(self) -> list:
            return []

    class FakeDb:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return EmptyResult()

    monkeypatch.setattr(search_api, "parse_query", fake_parse)
    monkeypatch.setattr(search_api, "get_query_embedding", fake_embedding)
    monkeypatch.setattr(search_api, "get_user_profile", fake_profile)

    db = FakeDb()
    response = await search_api.semantic_search(
        payload=SearchQuery(q="写着WELCOME的门垫", limit=5, auto_parse=True),
        current_user=SimpleNamespace(id=uuid4()),
        db=db,
    )
    assert response.items == []
    assert captured["effective_query"] == "写着WELCOME的门垫"
    assert "photo_tags" not in str(db.statement)


@pytest.mark.asyncio
async def test_agent_tool_auto_tags_do_not_become_hard_photo_tags(monkeypatch) -> None:
    import app.services.agent_tools as agent_tools

    captured: dict[str, object] = {}

    async def fake_parse(_: str) -> ParsedQuery:
        return ParsedQuery(semantic="猫", place=None, tags=["猫"])

    async def fake_embedding(text: str) -> tuple[list[float], bool]:
        captured["effective_query"] = text
        return [0.0] * 1024, False

    async def fake_profile(*_args):
        return None

    class EmptyResult:
        def all(self) -> list:
            return []

    class FakeDb:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return EmptyResult()

    monkeypatch.setattr(agent_tools, "parse_query", fake_parse)
    monkeypatch.setattr(agent_tools, "get_query_embedding", fake_embedding)
    monkeypatch.setattr(agent_tools, "get_user_profile", fake_profile)

    db = FakeDb()
    result = await agent_tools.search_photos(
        user_id=uuid4(),
        db=db,
        query="猫趴在电脑键盘上睡觉",
        limit=5,
        auto_parse=True,
    )
    assert result["ok"] is True
    assert result["items"] == []
    assert captured["effective_query"] == "猫趴在电脑键盘上睡觉"
    assert "photo_tags" not in str(db.statement)
