"""搜索结果模式：普通 Top-5、最佳单图与用户自选完整结果集。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.photo import SearchQuery
from app.services.search import (
    infer_complete_result_filters,
    resolve_search_result_limits,
)


def test_result_limits_keep_best_selection_pool_at_five() -> None:
    assert resolve_search_result_limits("browse", 20) == (5, 5)
    assert resolve_search_result_limits("browse", 3) == (3, 5)
    assert resolve_search_result_limits("best", 20) == (1, 5)
    assert resolve_search_result_limits("select", 30) == (30, 30)
    assert resolve_search_result_limits("select", 100) == (100, 100)
    assert resolve_search_result_limits("select", 1, complete_result_set=True) == (
        None,
        None,
    )


def test_complete_result_filters_use_reliable_analysis_fields() -> None:
    assert infer_complete_result_filters("把全部自拍给我") == {
        "photo_types": ["selfie"],
        "is_selfie": True,
        "people_count_min": None,
        "people_count_max": None,
        "all_album": False,
    }
    assert infer_complete_result_filters("所有手机截图") == {
        "photo_types": ["screenshot"],
        "is_selfie": None,
        "people_count_min": None,
        "people_count_max": None,
        "all_album": False,
    }
    assert infer_complete_result_filters("全部金毛照片") == {
        "photo_types": [],
        "is_selfie": None,
        "people_count_min": None,
        "people_count_max": None,
        "all_album": False,
    }
    assert infer_complete_result_filters("把所有照片都给我") == {
        "photo_types": [],
        "is_selfie": None,
        "people_count_min": None,
        "people_count_max": None,
        "all_album": True,
    }
    assert infer_complete_result_filters("全部合照") == {
        "photo_types": ["group_photo"],
        "is_selfie": None,
        "people_count_min": 2,
        "people_count_max": None,
        "all_album": False,
    }


def test_search_query_rejects_unknown_result_mode() -> None:
    with pytest.raises(ValidationError):
        SearchQuery(q="海边照片", result_mode="unknown")


def test_complete_result_set_requires_select_mode() -> None:
    with pytest.raises(ValidationError, match="仅支持 select"):
        SearchQuery(q="全部自拍", complete_result_set=True, result_mode="browse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_mode", "expected_count", "rerank_enabled", "page_limit"),
    [
        ("browse", 5, True, 5),
        ("best", 1, True, 5),
        ("select", 20, False, 20),
    ],
)
async def test_http_search_applies_explicit_result_mode(
    monkeypatch: pytest.MonkeyPatch,
    result_mode: str,
    expected_count: int,
    rerank_enabled: bool,
    page_limit: int,
) -> None:
    import app.api.search as search_api

    photos = [
        SimpleNamespace(
            id=uuid4(),
            thumb_key=f"thumb-{index}.jpg",
            oss_key=f"photo-{index}.jpg",
            taken_at=None,
            ai_description=f"候选照片 {index}",
            ai_analysis={},
            status="done",
        )
        for index in range(35)
    ]
    captured: dict[str, int] = {}

    async def fake_embedding(_text: str) -> tuple[list[float], bool]:
        return [0.0] * 1024, False

    async def fake_profile(*_args):
        return None

    async def fake_rerank(scored, _query, *, enabled, page_limit):
        captured["enabled"] = enabled
        captured["page_limit"] = page_limit
        return list(scored), None

    class Result:
        def all(self):
            return [(photo, index / 100) for index, photo in enumerate(photos)]

    class Db:
        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(search_api, "get_query_embedding", fake_embedding)
    monkeypatch.setattr(search_api, "get_user_profile", fake_profile)
    monkeypatch.setattr(search_api, "rerank_scored_candidates", fake_rerank)
    monkeypatch.setattr(search_api, "sign_get_url", lambda key: f"https://x/{key}")

    response = await search_api.semantic_search(
        SearchQuery(q="海边照片", limit=20, result_mode=result_mode),
        SimpleNamespace(id=uuid4()),
        Db(),
    )

    assert captured["enabled"] is rerank_enabled
    assert captured["page_limit"] == page_limit
    assert response.result_mode == result_mode
    assert response.total == expected_count
    assert len(response.items) == expected_count
    assert response.next_cursor is None
    assert response.items[0].id == photos[0].id


@pytest.mark.asyncio
async def test_http_select_complete_returns_every_scanned_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.search as search_api

    photos = [
        SimpleNamespace(
            id=uuid4(),
            thumb_key=f"thumb-{index}.jpg",
            oss_key=f"photo-{index}.jpg",
            taken_at=None,
            ai_description=f"自拍 {index}",
            ai_analysis={"capture_context": ["自拍"]},
            status="done",
        )
        for index in range(73)
    ]

    statements = []

    class Result:
        def all(self):
            return [(photo, index / 1000) for index, photo in enumerate(photos)]

    class Db:
        async def execute(self, statement):
            statements.append(statement)
            return Result()

    monkeypatch.setattr(
        search_api,
        "get_query_embedding",
        lambda _text: _async_value(([0.0] * 1024, False)),
    )
    monkeypatch.setattr(
        search_api, "get_user_profile", lambda *_args: _async_value(None)
    )
    monkeypatch.setattr(
        search_api,
        "rerank_scored_candidates",
        lambda scored, *_args, **_kwargs: _async_value((list(scored), None)),
    )
    monkeypatch.setattr(search_api, "sign_get_url", lambda key: f"https://x/{key}")

    response = await search_api.semantic_search(
        SearchQuery(
            q="全部自拍",
            result_mode="select",
            complete_result_set=True,
        ),
        SimpleNamespace(id=uuid4()),
        Db(),
    )

    assert response.total == 73
    assert response.total_matches == 73
    assert response.result_set_complete is True
    assert response.truncated is False
    compiled = statements[-1].compile()
    search_sql = str(compiled)
    assert ["selfie"] in compiled.params.values()
    assert "photo_type" in search_sql
    assert " LIMIT " not in search_sql.upper()


async def _async_value(value):
    return value
