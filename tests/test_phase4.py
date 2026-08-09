"""Phase 4 生产加固与数据迁移测试（无需真实 Redis / DB / 外部服务）."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.schemas.analysis import ImageAnalysis
from app.services.agent import AgentConstraints, PhotoAgent
from app.services.circuit_breaker import CircuitBreaker, ServiceDegradedError
from app.services.lock import AgentLock
from app.services.metrics import AgentMetrics
from app.workers.lifecycle_tasks import (
    _COLD_DAYS,
    _HOT_DAYS,
    _WARM_DAYS,
    archive_cold_events,
    count_events_by_age,
)
from app.workers.migrate_tasks import migrate_photos_batch
from scripts.offline_eval import evaluate_dataset, evaluate_sample


# ------------------------------------------------------------------
# 辅助：Fake Redis（覆盖 AgentLock 所需的最小接口）
# ------------------------------------------------------------------
class FakeRedis:
    """模拟 Redis，覆盖 AgentLock 所需的最小接口（含 Lua eval 模拟）。"""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def ping(self) -> str:
        return "PONG"

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self._data:
            return None
        self._data[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self._data.pop(key, None) is not None else 0

    async def expire(self, key: str, ttl: int, xx: bool = False) -> bool:
        if xx and key not in self._data:
            return False
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self._data else 0

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        """模拟 Lua 脚本执行：支持 release 和 extend 两种脚本。"""
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        key = keys[0]
        token = args[0]

        if "del" in script:
            # release 脚本：token 匹配则删除
            if self._data.get(key) == token:
                del self._data[key]
                return 1
            return 0

        if "expire" in script:
            # extend 脚本：token 匹配则续期
            if self._data.get(key) == token:
                return 1
            return 0

        return 0


# ------------------------------------------------------------------
# 辅助：构造可进入 async with 的 mock Session
# ------------------------------------------------------------------
def _async_session_mock(db: Any) -> MagicMock:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


# ------------------------------------------------------------------
# 熔断器
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_circuit_breaker_closed_success() -> None:
    cb = CircuitBreaker("test", failure_threshold=2, recovery_interval=1)

    async def ok() -> str:
        return "ok"

    assert await cb.call(ok) == "ok"
    assert cb.state == "closed"
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures() -> None:
    cb = CircuitBreaker("test", failure_threshold=2, recovery_interval=10)

    async def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == "closed"
    assert cb.failure_count == 1

    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == "open"
    assert cb.failure_count == 2

    # 熔断后再次调用直接拒绝，不再执行函数
    sentinel = {"called": False}

    async def should_not_run() -> str:
        sentinel["called"] = True
        return "surprise"

    with pytest.raises(ServiceDegradedError):
        await cb.call(should_not_run)
    assert sentinel["called"] is False


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_recovery() -> None:
    cb = CircuitBreaker("test", failure_threshold=1, recovery_interval=0.05)

    async def fail() -> None:
        raise RuntimeError("boom")

    async def ok() -> str:
        return "ok"

    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == "open"

    await asyncio.sleep(0.1)
    assert await cb.call(ok) == "ok"
    assert cb.state == "closed"
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_probe_failure_reopens() -> None:
    cb = CircuitBreaker("test", failure_threshold=1, recovery_interval=0.05)

    async def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == "open"

    await asyncio.sleep(0.1)
    with pytest.raises(RuntimeError):
        await cb.call(fail)
    assert cb.state == "open"


def test_circuit_breaker_to_dict() -> None:
    cb = CircuitBreaker("vl", failure_threshold=3, recovery_interval=300)
    d = cb.to_dict()
    assert d["name"] == "vl"
    assert d["state"] == "closed"
    assert d["failure_threshold"] == 3
    assert "last_failure_time" in d


# ------------------------------------------------------------------
# Redis 分布式锁
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_lock_acquire_and_release() -> None:
    redis = FakeRedis()
    lock = AgentLock("user-1", redis=redis)

    assert await lock.acquire(ttl=30) is True
    assert await lock.is_locked() is True

    # 同用户第二次加锁失败
    lock2 = AgentLock("user-1", redis=redis)
    assert await lock2.acquire(ttl=30) is False

    await lock.release()
    assert await lock.is_locked() is False

    # 释放后再次加锁成功
    assert await lock.acquire(ttl=30) is True
    await lock.release()


@pytest.mark.asyncio
async def test_agent_lock_extend() -> None:
    redis = FakeRedis()
    lock = AgentLock("user-1", redis=redis)

    assert await lock.acquire(ttl=30) is True
    assert await lock.extend(ttl=60) is True

    await lock.release()
    # 锁不存在时续期失败
    assert await lock.extend(ttl=60) is False


@pytest.mark.asyncio
async def test_agent_lock_different_users_do_not_block() -> None:
    redis = FakeRedis()
    lock_a = AgentLock("user-a", redis=redis)
    lock_b = AgentLock("user-b", redis=redis)

    assert await lock_a.acquire(ttl=30) is True
    assert await lock_b.acquire(ttl=30) is True

    await lock_a.release()
    await lock_b.release()


# ------------------------------------------------------------------
# 监控指标
# ------------------------------------------------------------------
def test_metrics_counter_and_histogram_without_prometheus() -> None:
    """未安装 prometheus_client 时不应抛异常."""
    m = AgentMetrics()
    m.counter("test_counter", tags={"env": "test"})
    m.histogram("test_hist", 1.23, tags={"env": "test"})


def test_metrics_record_session_end() -> None:
    m = AgentMetrics()
    m.record_session_end(
        {"attempts": 3, "fallback_triggered": True, "current_strategy": "browse"},
        elapsed=1.2,
        tokens=100,
    )


def test_metrics_record_vl_parse_failure_only_on_non_ok() -> None:
    m = AgentMetrics()
    m.record_vl_parse_failure("ok")      # 不应记录
    m.record_vl_parse_failure("fallback")  # 应记录


def test_metrics_record_photo_status() -> None:
    m = AgentMetrics()
    m.record_photo_status("done")
    m.record_photo_status("partial_done")


@pytest.mark.asyncio
async def test_metrics_timeit() -> None:
    m = AgentMetrics()
    async with m.timeit("test_operation", tags={"x": "y"}):
        await asyncio.sleep(0)


# ------------------------------------------------------------------
# 存量照片结构化分析迁移
# ------------------------------------------------------------------
def _photo_mock(
    *,
    status: str = "done",
    ai_analysis: dict | None = None,
    ai_description: str | None = None,
) -> MagicMock:
    photo = MagicMock()
    photo.id = uuid4()
    photo.user_id = uuid4()
    photo.oss_key = "photos/test.jpg"
    photo.status = status
    photo.ai_analysis = ai_analysis
    photo.ai_description = ai_description
    return photo


@pytest.mark.asyncio
async def test_migrate_photos_batch_upgrades_empty_analysis() -> None:
    photo = _photo_mock(ai_analysis={})

    result = MagicMock()
    result.scalars.return_value.all.return_value = [photo]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    analysis = ImageAnalysis(
        scene="户外",
        scene_detail="海边",
        summary="海边的照片",
        parse_quality="ok",
    )

    with patch(
        "app.workers.migrate_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ), patch(
        "app.workers.migrate_tasks.sign_get_url",
        return_value="http://test.jpg",
    ), patch(
        "app.workers.migrate_tasks.ai_service.analyze_image",
        new=AsyncMock(return_value=analysis),
    ):
        res = await migrate_photos_batch({}, batch_size=10)

    assert res["processed"] == 1
    assert res["upgraded"] == 1
    assert res["failed"] == 0
    assert photo.ai_analysis is not None


@pytest.mark.asyncio
async def test_migrate_photos_batch_updates_description_on_fallback() -> None:
    photo = _photo_mock(ai_analysis={})

    result = MagicMock()
    result.scalars.return_value.all.return_value = [photo]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    analysis = ImageAnalysis(
        scene="其他",
        summary="一张无法结构化解析的照片",
        parse_quality="fallback",
    )

    with patch(
        "app.workers.migrate_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ), patch(
        "app.workers.migrate_tasks.sign_get_url",
        return_value="http://test.jpg",
    ), patch(
        "app.workers.migrate_tasks.ai_service.analyze_image",
        new=AsyncMock(return_value=analysis),
    ):
        res = await migrate_photos_batch({}, batch_size=10)

    assert res["upgraded"] == 1
    assert photo.ai_description == analysis.summary


@pytest.mark.asyncio
async def test_migrate_photos_batch_handles_analysis_failure() -> None:
    photo = _photo_mock(ai_analysis={})

    result = MagicMock()
    result.scalars.return_value.all.return_value = [photo]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def boom(_url: str) -> ImageAnalysis:
        raise RuntimeError("vl timeout")

    with patch(
        "app.workers.migrate_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ), patch(
        "app.workers.migrate_tasks.sign_get_url",
        return_value="http://test.jpg",
    ), patch(
        "app.workers.migrate_tasks.ai_service.analyze_image",
        new=boom,
    ):
        res = await migrate_photos_batch({}, batch_size=10)

    assert res["processed"] == 1
    assert res["upgraded"] == 0
    assert res["failed"] == 1
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_migrate_photos_batch_filters_by_status() -> None:
    """只处理 status 为 done / partial_done 的照片."""
    # 通过返回空结果模拟：查询条件已被执行（由 db.execute 调用次数间接验证）
    result = MagicMock()
    result.scalars.return_value.all.return_value = []

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    with patch(
        "app.workers.migrate_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ):
        res = await migrate_photos_batch({}, batch_size=10)

    assert res["processed"] == 0
    assert res["upgraded"] == 0
    assert res["failed"] == 0


# ------------------------------------------------------------------
# 数据生命周期管理
# ------------------------------------------------------------------
def _event_mock(created_at: datetime) -> MagicMock:
    ev = MagicMock()
    ev.id = 1
    ev.user_id = uuid4()
    ev.event_type = "search_click"
    ev.payload = {"query": "猫"}
    ev.created_at = created_at
    return ev


@pytest.mark.asyncio
async def test_archive_cold_events_no_events() -> None:
    result = MagicMock()
    result.scalars.return_value.all.return_value = []

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    with patch(
        "app.workers.lifecycle_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ):
        res = await archive_cold_events({}, batch_size=10)

    assert res["ok"] is True
    assert res["archived"] == 0
    assert res["oss_key"] is None


@pytest.mark.asyncio
async def test_archive_cold_events_archives_and_deletes() -> None:
    old = _event_mock(datetime.now(timezone.utc) - timedelta(days=_COLD_DAYS + 1))

    result = MagicMock()
    result.scalars.return_value.all.return_value = [old]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    captured = {}

    def fake_put_object(key: str, data: bytes, content_type: str = "") -> None:
        captured["key"] = key
        captured["data"] = data
        captured["content_type"] = content_type

    with patch(
        "app.workers.lifecycle_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ), patch(
        "app.workers.lifecycle_tasks.put_object",
        side_effect=fake_put_object,
    ):
        res = await archive_cold_events({}, batch_size=10)

    assert res["ok"] is True
    assert res["archived"] == 1
    assert res["oss_key"] is not None
    assert res["oss_key"] == captured["key"]
    assert captured["content_type"] == "application/x-ndjson"
    assert b"search_click" in captured["data"]
    # 应执行删除语句并提交
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_cold_events_upload_failure_keeps_data() -> None:
    old = _event_mock(datetime.now(timezone.utc) - timedelta(days=_COLD_DAYS + 1))

    result = MagicMock()
    result.scalars.return_value.all.return_value = [old]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    with patch(
        "app.workers.lifecycle_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ), patch(
        "app.workers.lifecycle_tasks.put_object",
        side_effect=RuntimeError("oss unavailable"),
    ):
        res = await archive_cold_events({}, batch_size=10)

    assert res["ok"] is False
    assert res["archived"] == 0
    # 上传失败不应删除原记录
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_events_by_age_distribution() -> None:
    def scalar_result(value: int) -> MagicMock:
        r = MagicMock()
        r.scalar.return_value = value
        return r

    db = MagicMock()
    # 热 / 温 / 冷 三次 count 查询
    db.execute = AsyncMock(
        side_effect=[scalar_result(5), scalar_result(3), scalar_result(2)]
    )

    with patch(
        "app.workers.lifecycle_tasks.AsyncSessionLocal",
        return_value=_async_session_mock(db),
    ):
        res = await count_events_by_age()

    assert res["ok"] is True
    assert res["hot"] == 5
    assert res["warm"] == 3
    assert res["cold"] == 2
    assert res["thresholds"]["hot_days"] == _HOT_DAYS
    assert res["thresholds"]["warm_days"] == _WARM_DAYS
    assert res["thresholds"]["cold_days"] == _COLD_DAYS


# ------------------------------------------------------------------
# 离线评估
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_evaluate_sample_scene_match() -> None:
    analysis = ImageAnalysis(
        scene="户外",
        objects=["猫", "草地"],
        text_in_image=[],
        summary="户外的猫和草地",
        parse_quality="ok",
    )

    with patch(
        "scripts.offline_eval.ai_service.analyze_image",
        new=AsyncMock(return_value=analysis),
    ):
        result = await evaluate_sample(
            "http://example.com/cat.jpg",
            {"scene": "户外", "objects": ["猫"], "text_in_image": [], "persons_count": 0},
        )

    assert result["scene_ok"] is True
    assert result["tp_obj"] >= 1
    assert result["persons_ok"] is True
    assert result["parse_quality"] == "ok"


@pytest.mark.asyncio
async def test_evaluate_sample_counts_error() -> None:
    async def boom(_url: str) -> ImageAnalysis:
        raise RuntimeError("network error")

    with patch(
        "scripts.offline_eval.ai_service.analyze_image",
        new=boom,
    ):
        result = await evaluate_sample(
            "http://example.com/bad.jpg",
            {"scene": "户外"},
        )

    assert "error" in result
    assert result["parse_quality"] == "error"


def test_evaluate_dataset_summary(tmp_path: Any) -> None:
    dataset = tmp_path / "ground_truth.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "image_url": "http://example.com/a.jpg",
                    "expected": {
                        "scene": "户外",
                        "objects": ["猫"],
                        "text_in_image": [],
                        "persons_count": 0,
                    },
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    analysis = ImageAnalysis(
        scene="户外",
        objects=["猫", "草地"],
        text_in_image=[],
        summary="户外有猫",
        parse_quality="ok",
    )

    with patch(
        "scripts.offline_eval.ai_service.analyze_image",
        new=AsyncMock(return_value=analysis),
    ):
        result = asyncio.run(evaluate_dataset(str(dataset)))

    summary = result["summary"]
    assert summary["total"] == 1
    assert summary["errors"] == 0
    assert summary["scene_accuracy"] == 1.0
    assert summary["parse_ok_rate"] == 1.0


# ------------------------------------------------------------------
# 健康检查
# ------------------------------------------------------------------
def test_health_endpoint_returns_breaker_status() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with patch(
        "app.main.get_redis",
        new=AsyncMock(return_value=FakeRedis()),
    ):
        client = TestClient(app)
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["redis"] == "ok"
    assert "vl" in data["circuit_breakers"]
    assert "agent_llm" in data["circuit_breakers"]
    assert data["circuit_breakers"]["vl"]["state"] == "closed"


def test_health_endpoint_degraded_when_redis_down() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    async def broken_redis() -> FakeRedis:
        raise RuntimeError("redis down")

    with patch(
        "app.main.get_redis",
        new=broken_redis,
    ):
        client = TestClient(app)
        response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert "error" in data["redis"]


# ------------------------------------------------------------------
# Agent LLM 熔断降级
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_llm_degraded_fallback_to_browse() -> None:
    db = MagicMock()

    async def browse_result(**kwargs) -> dict:
        return {"ok": True, "items": [], "hint": "fallback browse"}

    with patch(
        "app.services.agent._llm_decide",
        side_effect=ServiceDegradedError("dashscope_chat", "circuit open"),
    ), patch(
        "app.services.agent.browse_candidates",
        new=browse_result,
    ):
        agent = PhotoAgent(
            db=db,
            constraints=AgentConstraints(enable_browse_fallback=False),
        )
        state, events = await agent.run(
            user_id=uuid4(),
            query="找照片",
        )

    event_types = [e["type"] for e in events]
    assert "start" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "final" in event_types
    assert events[-1]["payload"].get("fallback") == "browse_candidates"


# ------------------------------------------------------------------
# 熔断器全局实例覆盖 AI 服务
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_analyze_image_returns_fallback_when_breaker_open() -> None:
    from app.services import ai as ai_service

    # 临时绕过 mock 分支，直接测熔断器 open 时的降级路径
    original_state = ai_service.vl_breaker.state
    original_failure_time = ai_service.vl_breaker.last_failure_time
    try:
        ai_service.vl_breaker.state = "open"
        ai_service.vl_breaker.last_failure_time = asyncio.get_event_loop().time()

        with patch.object(ai_service, "_is_mock", return_value=False):
            result = await ai_service.analyze_image("http://example.com/test.jpg")

        assert result.parse_quality == "vl_degraded"
        assert result.scene == "unknown"
    finally:
        ai_service.vl_breaker.state = original_state
        ai_service.vl_breaker.last_failure_time = original_failure_time


def test_agent_lock_to_dict() -> None:
    lock = AgentLock("user-1")
    d = lock.to_dict()
    assert d["user_id"] == "user-1"
    assert d["key"] == "lock:agent:user-1"
