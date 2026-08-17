"""Embedding 补算、熔断和客户端状态的回归测试。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.circuit_breaker import CircuitBreaker, ServiceDegradedError
from app.services.search_index import (
    enqueue_index_repairs,
    get_index_coverage,
    processing_status,
    retry_delay_after_failure,
)
from app.workers.tasks import retry_photo_embedding


def _session(photo):
    result = MagicMock()
    result.scalar_one_or_none.return_value = photo
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=None)
    return session, db


def _photo(*, retry_count: int = 0):
    analysis = {
        "scene": "户外",
        "objects": ["树"],
        "summary": "一张阳光下的户外风景照片",
        "parse_quality": "ok",
    }
    return SimpleNamespace(
        id=uuid4(),
        status="partial_done",
        partial_reason="embedding_retrying",
        embedding=None,
        ai_description="阳光下的公园里有一棵绿色大树",
        ai_analysis=analysis,
        embedding_retry_count=retry_count,
        embedding_next_retry_at=datetime.now(timezone.utc),
        embedding_last_attempt_at=None,
        embedding_last_error=None,
    )


def test_retry_delays_are_relative_to_each_actual_failure() -> None:
    assert [retry_delay_after_failure(i) for i in range(1, 6)] == [
        2,
        8,
        25,
        60,
        None,
    ]


def test_processing_status_exposes_short_poll_countdown() -> None:
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    photo = SimpleNamespace(
        id=uuid4(),
        status="partial_done",
        search_index_status="retrying",
        search_index_message="智能搜索服务繁忙，正在继续尝试",
        embedding_retry_count=2,
        embedding_next_retry_at=now + timedelta(seconds=8),
    )
    status = processing_status(photo, now=now)
    assert status["retry_count"] == 2
    assert status["max_attempts"] == 5
    assert status["next_retry_in_seconds"] == 8


@pytest.mark.asyncio
async def test_index_coverage_marks_partial_album_incomplete() -> None:
    result = MagicMock()
    result.one.return_value = (3, 2, 0)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    coverage = await get_index_coverage(db, uuid4())

    assert coverage["coverage_ratio"] == pytest.approx(2 / 3, abs=0.0001)
    assert coverage["unavailable_photos"] == 1
    assert coverage["complete"] is False


@pytest.mark.asyncio
async def test_enqueue_index_repairs_marks_jobs_before_enqueue() -> None:
    photo = SimpleNamespace(
        id=uuid4(),
        partial_reason="embedding_retry_exhausted",
        embedding_next_retry_at=None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [photo]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    with patch(
        "app.workers.tasks.enqueue_retry_photo_embedding",
        new=AsyncMock(return_value=True),
    ) as enqueue:
        queued = await enqueue_index_repairs(db, uuid4())

    assert queued == 1
    assert photo.partial_reason == "embedding_retrying"
    db.commit.assert_awaited_once()
    enqueue.assert_awaited_once_with(photo.id)


@pytest.mark.asyncio
async def test_half_open_only_allows_one_real_probe() -> None:
    breaker = CircuitBreaker("embedding", failure_threshold=1, recovery_interval=0)
    breaker.state = "open"
    breaker.last_failure_time = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def probe() -> str:
        entered.set()
        await release.wait()
        return "ok"

    first = asyncio.create_task(breaker.call(probe))
    await entered.wait()
    with pytest.raises(ServiceDegradedError):
        await breaker.call(probe)
    release.set()
    assert await first == "ok"
    assert breaker.state == "closed"


@pytest.mark.asyncio
async def test_circuit_rejection_does_not_consume_photo_attempt() -> None:
    photo = _photo(retry_count=2)
    photo.partial_reason = "embedding_service_busy"
    session, _db = _session(photo)
    redis = SimpleNamespace(enqueue_job=AsyncMock(return_value=object()))

    with patch(
        "app.workers.tasks.AsyncSessionLocal", return_value=session
    ), patch(
        "app.workers.tasks.ai_service.embed_text",
        new=AsyncMock(side_effect=ServiceDegradedError("dashscope_embedding")),
    ), patch(
        "app.workers.tasks.embedding_breaker.retry_after_seconds",
        return_value=30,
    ):
        result = await retry_photo_embedding(
            {"redis": redis}, str(photo.id)
        )

    assert result["attempted"] is False
    assert photo.embedding_retry_count == 2
    assert result["retry_in_seconds"] == 30
    assert redis.enqueue_job.await_args.kwargs["_defer_by"] == 30


@pytest.mark.asyncio
async def test_fifth_real_failure_exhausts_but_keeps_vl_results() -> None:
    photo = _photo(retry_count=4)
    original_description = photo.ai_description
    original_analysis = photo.ai_analysis.copy()
    session, _db = _session(photo)

    with patch(
        "app.workers.tasks.AsyncSessionLocal", return_value=session
    ), patch(
        "app.workers.tasks.ai_service.embed_text",
        new=AsyncMock(side_effect=TimeoutError("embedding timeout")),
    ):
        result = await retry_photo_embedding(
            {"redis": SimpleNamespace()}, str(photo.id)
        )

    assert result["reason"] == "embedding_retry_exhausted"
    assert photo.embedding_retry_count == 5
    assert photo.status == "partial_done"
    assert photo.embedding is None
    assert photo.ai_description == original_description
    assert photo.ai_analysis == original_analysis


@pytest.mark.asyncio
async def test_embedding_retry_success_marks_search_ready() -> None:
    photo = _photo(retry_count=2)
    session, _db = _session(photo)
    vector = [0.01] * 1024

    with patch(
        "app.workers.tasks.AsyncSessionLocal", return_value=session
    ), patch(
        "app.workers.tasks.ai_service.embed_text",
        new=AsyncMock(return_value=vector),
    ):
        result = await retry_photo_embedding(
            {"redis": SimpleNamespace()}, str(photo.id)
        )

    assert result == {"ok": True, "status": "done", "retry_count": 0}
    assert photo.status == "done"
    assert photo.embedding == vector
    assert photo.partial_reason is None
    assert photo.embedding_retry_count == 0
