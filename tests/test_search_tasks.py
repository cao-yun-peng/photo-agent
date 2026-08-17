from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.search_tasks import prefetch_search_candidates


class FakeSessionContext:
    async def __aenter__(self) -> MagicMock:
        return MagicMock()

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.mark.asyncio
async def test_prefetch_search_candidates_stores_only_unique_unshown_matches() -> None:
    result = {
        "ok": True,
        "items": [
            {"id": "shown", "ai_description": "已展示"},
            {"id": "photo-3", "ai_description": "切尔西第三张"},
        ],
        "_candidate_pool_items": [
            {"id": "photo-3", "ai_description": "重复"},
            {"id": "photo-4", "ai_description": "切尔西第四张"},
        ],
    }
    statuses: list[str] = []

    async def record_status(_session_id: str, status: str) -> None:
        statuses.append(status)

    push = AsyncMock(return_value=2)
    search = AsyncMock(return_value=result)
    with (
        patch(
            "app.workers.search_tasks.AsyncSessionLocal",
            new=lambda: FakeSessionContext(),
        ),
        patch("app.workers.search_tasks.search_photos", new=search),
        patch("app.workers.search_tasks.push_verified_candidates", new=push),
        patch("app.workers.search_tasks.set_prefetch_status", new=record_status),
    ):
        outcome = await prefetch_search_candidates(
            {},
            "session-1",
            "13b58a51-5fb8-4ae7-98ee-30611de255df",
            "切尔西相关照片",
            ["shown"],
        )

    assert outcome == {"ok": True, "verified_count": 2}
    assert statuses == ["running", "ready"]
    assert [item["id"] for item in push.await_args.args[1]] == [
        "photo-3",
        "photo-4",
    ]
    called = search.await_args.kwargs
    assert called["verified_only"] is True
    assert called["force_visual_verify"] is True
    assert called["exclude_photo_ids"] == ["shown"]


@pytest.mark.asyncio
async def test_prefetch_search_candidates_marks_failed_search() -> None:
    statuses: list[str] = []

    async def record_status(_session_id: str, status: str) -> None:
        statuses.append(status)

    with (
        patch(
            "app.workers.search_tasks.AsyncSessionLocal",
            new=lambda: FakeSessionContext(),
        ),
        patch(
            "app.workers.search_tasks.search_photos",
            new=AsyncMock(return_value={"ok": False, "error_type": "timeout"}),
        ),
        patch("app.workers.search_tasks.set_prefetch_status", new=record_status),
    ):
        outcome = await prefetch_search_candidates(
            {},
            "session-1",
            "13b58a51-5fb8-4ae7-98ee-30611de255df",
            "切尔西相关照片",
            [],
        )

    assert outcome == {"ok": False, "reason": "timeout"}
    assert statuses == ["running", "failed"]
