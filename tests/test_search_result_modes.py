"""搜索结果模式：普通 Top-5 与最佳单图。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.photo import SearchQuery
from app.services.search import resolve_search_result_limits


def test_result_limits_keep_best_selection_pool_at_five() -> None:
    assert resolve_search_result_limits("browse", 20) == (5, 5)
    assert resolve_search_result_limits("browse", 3) == (3, 5)
    assert resolve_search_result_limits("best", 20) == (1, 5)


def test_search_query_rejects_unknown_result_mode() -> None:
    with pytest.raises(ValidationError):
        SearchQuery(q="海边照片", result_mode="unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_mode", "expected_count"),
    [("browse", 5), ("best", 1)],
)
async def test_http_search_applies_explicit_result_mode(
    monkeypatch: pytest.MonkeyPatch,
    result_mode: str,
    expected_count: int,
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
        for index in range(8)
    ]
    captured: dict[str, int] = {}

    async def fake_embedding(_text: str) -> tuple[list[float], bool]:
        return [0.0] * 1024, False

    async def fake_profile(*_args):
        return None

    async def fake_rerank(scored, _query, *, enabled, page_limit):
        assert enabled is True
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

    assert captured["page_limit"] == 5
    assert response.result_mode == result_mode
    assert response.total == expected_count
    assert len(response.items) == expected_count
    assert response.next_cursor is None
    assert response.items[0].id == photos[0].id
