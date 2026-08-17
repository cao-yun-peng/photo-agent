from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.search_candidate_pool import (
    candidate_pool_size,
    get_prefetch_status,
    pop_verified_candidate,
    push_verified_candidates,
    set_prefetch_status,
    wait_for_verified_candidate,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)
            self.lists.pop(key, None)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def rpush(self, key: str, *values: str) -> int:
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def expire(self, _key: str, _ttl: int) -> bool:
        return True

    async def lpop(self, key: str) -> str | None:
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))


@pytest.mark.asyncio
async def test_candidate_pool_preserves_order_and_consumes_once() -> None:
    redis = FakeRedis()
    with patch(
        "app.services.search_candidate_pool.get_redis",
        new=AsyncMock(return_value=redis),
    ):
        await set_prefetch_status("session-1", "ready")
        pushed = await push_verified_candidates(
            "session-1", [{"id": "photo-3"}, {"id": "photo-4"}]
        )
        first = await pop_verified_candidate("session-1")
        second = await pop_verified_candidate("session-1")
        third = await pop_verified_candidate("session-1")

        assert pushed == 2
        assert first == {"id": "photo-3"}
        assert second == {"id": "photo-4"}
        assert third is None
        assert await candidate_pool_size("session-1") == 0
        assert await get_prefetch_status("session-1") == "ready"


@pytest.mark.asyncio
async def test_wait_skips_corrupt_entry_and_returns_verified_candidate() -> None:
    redis = FakeRedis()
    redis.values["agent:search-prefetch-status:session-2"] = "running"
    redis.lists["agent:search-pool:session-2"] = [
        "not-json",
        json.dumps({"id": "photo-3"}),
    ]
    with patch(
        "app.services.search_candidate_pool.get_redis",
        new=AsyncMock(return_value=redis),
    ):
        item = await wait_for_verified_candidate("session-2", timeout_seconds=0)

    assert item == {"id": "photo-3"}
