"""Real PostgreSQL/Redis tests, never run against the application's .env."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.config import Config
from arq import Retry
from redis.asyncio import Redis
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.schema_contract import normalize_drift
from app.config import settings
from app.database import Base, OwnedAsyncSession
from app.models.photo import Photo
from app.models.user import User
from app.services import agent_ownership, lock, search_candidate_pool as pools
from app.services.task_lifecycle import OwnershipLost
from app.workers import photo_recovery, tasks, search_tasks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]


@pytest_asyncio.fixture
async def infra(monkeypatch):
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    assert (
        url.host in {"localhost", "127.0.0.1"}
        and url.database == "photo_agent_batch1_test"
    ), "Refusing non-test database"
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(
        engine, class_=OwnedAsyncSession, expire_on_commit=False, autoflush=False
    )
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    for module in (photo_recovery, tasks, search_tasks):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    from app.api import agent

    monkeypatch.setattr(agent, "AsyncSessionLocal", factory)
    monkeypatch.setattr(lock, "_redis_client", redis)
    user_id = uuid4()
    async with factory() as db:
        db.add(User(id=user_id, wechat_openid=f"batch1-{user_id}"))
        await db.commit()
    yield factory, redis, user_id
    async with factory() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
    await redis.aclose()
    await engine.dispose()


async def add_photo(factory, user_id, **values):
    photo_id = uuid4()
    async with factory() as db:
        db.add(
            Photo(
                id=photo_id,
                user_id=user_id,
                hash=uuid4().hex,
                oss_key=f"photos/{user_id}/{photo_id}.jpg",
                **values,
            )
        )
        await db.commit()
    return str(photo_id)


class Queue:
    def __init__(self, fail=False):
        self.jobs = []
        self.fail = fail

    async def enqueue_job(self, *args, **kwargs):
        if self.fail:
            raise ConnectionError("test queue unavailable")
        self.jobs.append((args, kwargs))
        return object()


@pytest.mark.asyncio
async def test_photo_claim_is_atomic_and_old_writer_is_fenced(infra):
    factory, _, user = infra
    pid = await add_photo(factory, user)
    claims = await asyncio.gather(
        photo_recovery.claim_photo(pid), photo_recovery.claim_photo(pid)
    )
    winner = next(c for c in claims if c is not None)
    assert sum(c is not None for c in claims) == 1
    old_token, _ = winner
    async with factory() as old:
        photo = await old.get(Photo, UUID(pid))
        old.info["commit_guard"] = photo_recovery.photo_commit_guard(
            pid, old_token, asyncio.Event()
        )
        async with factory() as db:
            await db.execute(
                update(Photo)
                .where(Photo.id == photo.id)
                .values(processing_lease_until=func.now() - timedelta(seconds=1))
            )
            await db.commit()
        new = await photo_recovery.claim_photo(pid)
        assert new and new[0] != old_token
        photo.ai_description = "stale write"
        with pytest.raises(OwnershipLost):
            await old.commit()
    async with factory() as db:
        assert (await db.get(Photo, UUID(pid))).ai_description is None


@pytest.mark.asyncio
async def test_durable_recovery_repairs_enqueue_failure_and_exhaustion(
    infra, monkeypatch
):
    factory, _, user = infra
    pid = await add_photo(factory, user)
    token, _ = await photo_recovery.claim_photo(pid)
    async with factory() as db:
        await db.execute(
            update(Photo)
            .where(Photo.processing_token == token)
            .values(processing_lease_until=func.now() - timedelta(seconds=1))
        )
        await db.commit()
    failed = await photo_recovery.recover_photo_jobs({"redis": Queue(fail=True)})
    assert failed["queued"] == 0
    queue = Queue()
    recovered = await photo_recovery.recover_photo_jobs({"redis": queue})
    assert recovered["queued"] >= 1
    assert any(job[0][1] == pid for job in queue.jobs)
    assert await photo_recovery.claim_photo(pid) is not None
    async with factory() as db:
        await db.execute(
            update(Photo)
            .where(Photo.id == UUID(pid))
            .values(
                processing_attempts=settings.photo_processing_max_attempts,
                processing_lease_until=func.now() - timedelta(seconds=1),
            )
        )
        await db.commit()
    await photo_recovery.recover_photo_jobs({"redis": Queue()})
    async with factory() as db:
        photo = await db.get(Photo, UUID(pid))
        assert photo.status == "failed"
        assert photo.partial_reason == "process_retry_exhausted"
    assert await photo_recovery.claim_photo(pid) is None


@pytest.mark.asyncio
async def test_worker_explicit_retry_and_cancel_state(infra, monkeypatch):
    factory, _, user = infra
    pid = await add_photo(factory, user)

    async def fail(*args):
        raise ConnectionError("transient")

    monkeypatch.setattr(tasks, "_process_claimed_photo", fail)
    with pytest.raises(Retry):
        await tasks.process_photo({"redis": Queue()}, pid)
    async with factory() as db:
        photo = await db.get(Photo, UUID(pid))
        assert photo.status == "pending" and photo.processing_attempts == 1
    entered = asyncio.Event()

    async def block(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tasks, "_process_claimed_photo", block)
    pid2 = await add_photo(factory, user)
    task = asyncio.create_task(tasks.process_photo({"redis": Queue()}, pid2))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with factory() as db:
        photo = await db.get(Photo, UUID(pid2))
        assert photo.status == "pending" and photo.processing_token is None


@pytest.mark.asyncio
async def test_worker_retry_reuses_saved_vl(infra, monkeypatch):
    from app.schemas.analysis import ImageAnalysis
    from types import SimpleNamespace

    factory, _, user = infra
    pid = await add_photo(factory, user)
    calls = {"describe": 0, "analyze": 0}

    async def get_object(*args):
        return b"test"

    async def put_object(*args, **kwargs):
        return None

    async def describe(*args):
        calls["describe"] += 1
        return "A detailed description of a cat sitting on a sofa."

    async def analyze(*args):
        calls["analyze"] += 1
        if calls["analyze"] == 1:
            raise ConnectionError("retry after description checkpoint")
        return ImageAnalysis(
            analysis_version=tasks.ai_service.VL_ANALYSIS_PROMPT_VERSION
        )

    async def embedding(*args):
        return [0.01] * 1024

    monkeypatch.setattr(tasks, "get_object", get_object)
    monkeypatch.setattr(tasks, "put_object", put_object)
    monkeypatch.setattr(tasks, "preflight_check", lambda raw: SimpleNamespace(ok=True))
    monkeypatch.setattr(
        tasks.image_service,
        "process",
        lambda *a, **kw: SimpleNamespace(
            thumb_bytes=b"x", width=100, height=100, taken_at=None, location=None
        ),
    )
    monkeypatch.setattr(tasks.ai_service, "describe_image", describe)
    monkeypatch.setattr(tasks.ai_service, "analyze_image", analyze)
    monkeypatch.setattr(tasks.ai_service, "embed_text", embedding)
    with pytest.raises(Retry):
        await tasks.process_photo({"redis": Queue()}, pid)
    async with factory() as db:
        await db.execute(
            update(Photo)
            .where(Photo.id == UUID(pid))
            .values(processing_retry_at=func.now())
        )
        await db.commit()
    result = await tasks.process_photo({"redis": Queue()}, pid)
    assert result.get("status") in {"done", "partial_done"}
    assert calls == {"describe": 1, "analyze": 2}


@pytest.mark.asyncio
async def test_agent_persistent_fence_rejects_old_run(infra):
    factory, redis, user = infra
    first = lock.AgentLock(str(user), redis)
    assert await first.acquire()
    async with factory() as old:
        await agent_ownership.claim_agent_run(old, user, first)
        await first.release()
        second = lock.AgentLock(str(user), redis)
        assert await second.acquire()
        async with factory() as new:
            await agent_ownership.claim_agent_run(new, user, second)

        # Even if the Redis check is fooled, PostgreSQL rejects the old token.
        async def apparently_owned():
            return None

        first.assert_owned = apparently_owned
        stale = await old.get(User, user)
        stale.nickname = "old"
        with pytest.raises(OwnershipLost):
            await old.commit()
        async with factory() as db:
            assert (await db.get(User, user)).nickname is None
        await second.release()


@pytest.mark.asyncio
async def test_agent_lock_loss_cancels_execution_and_closes_session(infra, monkeypatch):
    from app.api import agent
    from app.schemas.agent import AgentRunRequest
    from fastapi import HTTPException

    _, redis, user = infra
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def execute(db, *args):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(agent, "_execute_agent", execute)
    monkeypatch.setattr(settings, "agent_lock_ttl", 1)
    task = asyncio.create_task(
        agent._run_agent_with_lock(user, AgentRunRequest(query="cat"))
    )
    await asyncio.wait_for(entered.wait(), 3)
    await redis.set(f"lock:agent:{user}", "replacement", ex=10)
    with pytest.raises(HTTPException) as exc:
        await asyncio.wait_for(task, 3)
    assert exc.value.status_code == 503
    assert stopped.is_set()
    assert await redis.get(f"lock:agent:{user}") == "replacement"


@pytest.mark.asyncio
async def test_prefetch_generation_atomicity_and_idempotency(infra, monkeypatch):
    _, _, user = infra
    session = uuid4()
    first = await pools.begin_candidate_search(user, session)
    queue = Queue()
    monkeypatch.setattr(search_tasks, "_pool", queue)
    assert await search_tasks.enqueue_search_prefetch(
        session_id=first,
        user_id=user,
        query="cat",
        exclude_photo_ids=[],
        search_options={"plan_id": str(session)},
    )
    assert (
        await pools.push_verified_candidates(
            first, [{"id": str(uuid4()), "thumb_url": "secret-capability"}]
        )
        == 1
    )
    assert await search_tasks.enqueue_search_prefetch(
        session_id=first,
        user_id=user,
        query="cat",
        exclude_photo_ids=[],
        search_options={"plan_id": str(session)},
    )
    assert len(queue.jobs) == 1 and await pools.candidate_pool_size(first) == 1
    second = await pools.begin_candidate_search(user, session)
    assert await pools.push_verified_candidates(first, [{"id": str(uuid4())}]) == 0
    assert not await pools.set_prefetch_status(first, "failed")
    assert await pools.pop_verified_candidate(first) is None
    assert await pools.get_prefetch_status(second) == "missing"
    photo = str(uuid4())
    await pools.push_verified_candidates(
        second, [{"id": photo, "thumb_url": "expired"}]
    )
    one, two = await asyncio.gather(
        pools.pop_verified_candidate(second), pools.pop_verified_candidate(second)
    )
    assert sum(item is not None for item in (one, two)) == 1
    assert "thumb_url" not in (one or two)
    legacy = await search_tasks.prefetch_search_candidates(
        {}, str(session), str(user), "cat", []
    )
    assert legacy["reason"] == "legacy_job_discarded"


@pytest.mark.asyncio
async def test_prefetch_worker_cannot_publish_late_results(infra, monkeypatch):
    _, _, user = infra
    session = uuid4()
    first = await pools.begin_candidate_search(user, session)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def search(self, request, **kwargs):
        assert kwargs["prefetch"] is True
        assert request.tags == ["cat"]
        entered.set()
        await release.wait()
        return {"ok": True, "items": [{"id": str(uuid4())}]}

    monkeypatch.setattr(search_tasks.SearchService, "search", search)
    monkeypatch.setattr(settings, "search_visual_verify_enabled", False)
    task = asyncio.create_task(
        search_tasks.prefetch_search_candidates(
            {},
            first,
            str(user),
            "cat",
            [],
            {"tags": ["cat"], "auto_parse": False, "plan_id": str(session)},
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    second = await pools.begin_candidate_search(user, session)
    release.set()
    result = await task
    assert not result["ok"]
    assert await pools.candidate_pool_size(second) == 0


@pytest.mark.asyncio
async def test_migration_has_one_head_and_no_new_schema_drift(infra):
    factory, _, _ = infra
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        "20260912_0001"
    ]
    async with factory() as db:
        conn = await db.connection()

        def drift(sync):
            context = MigrationContext.configure(sync, opts={"compare_type": True})
            return render_python_code(
                produce_migrations(context, Base.metadata).upgrade_ops,
                migration_context=context,
            )

        actual = await conn.run_sync(drift)
        expected = Path("tests/schema_drift_baseline.txt").read_text(encoding="utf-8")
        assert (
            normalize_drift(actual).strip() == expected.strip()
        ), "New schema drift: inspect it; do not blindly regenerate the baseline"


@pytest.mark.asyncio
async def test_killed_worker_is_recovered_without_cleanup(infra, monkeypatch):
    import sys

    factory, _, user = infra
    pid = await add_photo(factory, user)
    code = """
import asyncio, sys
from app.workers import tasks
async def block(*args):
    print("CLAIMED", flush=True)
    await asyncio.Event().wait()
tasks._process_claimed_photo = block
asyncio.run(tasks.process_photo({}, sys.argv[1]))
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        pid,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (
            await asyncio.wait_for(process.stdout.readline(), 10)
        ).strip() == b"CLAIMED"
        process.kill()  # Our test child only: no cancellation handler can execute.
        await asyncio.wait_for(process.wait(), 5)
        async with factory() as db:
            photo = await db.get(Photo, UUID(pid))
            assert photo.status == "processing" and photo.processing_token is not None
            # Advance the lease deadline to avoid a minute-long test sleep.
            await db.execute(
                update(Photo)
                .where(Photo.id == UUID(pid))
                .values(processing_lease_until=func.now() - timedelta(seconds=1))
            )
            await db.commit()
        queue = Queue()
        await photo_recovery.recover_photo_jobs({"redis": queue})
        assert any(job[0][1] == pid for job in queue.jobs)

        async def complete(ctx, photo_id, db):
            photo = await db.get(Photo, UUID(photo_id))
            photo.status = "done"
            await db.commit()
            return {"ok": True, "status": "done"}

        monkeypatch.setattr(tasks, "_process_claimed_photo", complete)
        assert (await tasks.process_photo({"redis": queue}, pid))["ok"]
        async with factory() as db:
            photo = await db.get(Photo, UUID(pid))
            assert photo.status == "done" and photo.processing_attempts == 2
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_real_asgi_disconnect_after_request_dependency_closed(infra, monkeypatch):
    import json
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api import agent
    from app.core.security import get_current_user

    factory, redis, user = infra
    request_closed = asyncio.Event()
    execution_closed = asyncio.Event()
    first_frame = asyncio.Event()
    disconnect = asyncio.Event()

    async def authenticate():
        try:
            yield SimpleNamespace(id=user)
        finally:
            request_closed.set()

    async def execute(db, user_id, payload, queue):
        try:
            assert request_closed.is_set()  # FastAPI 0.115.0 dependency cleanup.
            assert await db.get(User, user_id) is not None
            await queue.put({"type": "start", "payload": {}})
            await asyncio.Event().wait()
        finally:
            execution_closed.set()

    monkeypatch.setattr(agent, "_execute_agent", execute)
    app = FastAPI()
    app.include_router(agent.router, prefix="/agent")
    app.dependency_overrides[get_current_user] = authenticate
    received_request = False

    async def receive():
        nonlocal received_request
        if not received_request:
            received_request = True
            return {
                "type": "http.request",
                "body": json.dumps({"query": "cat"}).encode(),
                "more_body": False,
            }
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_frame.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/stream",
        "raw_path": b"/agent/stream",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "root_path": "",
    }
    call = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(first_frame.wait(), 5)
        disconnect.set()
        await asyncio.wait_for(call, 5)
        assert execution_closed.is_set()
        assert await redis.get(f"lock:agent:{user}") is None
        async with factory() as db:
            assert await db.get(User, user) is not None
    finally:
        if not call.done():
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_scanners_share_dispatch_lease(infra):
    factory, _, user = infra
    pid = await add_photo(factory, user)
    queue = Queue()
    await asyncio.gather(
        photo_recovery.recover_photo_jobs({"redis": queue}),
        photo_recovery.recover_photo_jobs({"redis": queue}),
    )
    await photo_recovery.recover_photo_jobs({"redis": queue})
    assert sum(job[0][1] == pid for job in queue.jobs) == 1


@pytest.mark.asyncio
async def test_batch2_http_and_tool_local_date_and_tie_order(infra, monkeypatch):
    from datetime import date, datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.api import search as http_search
    from app.services import agent_tools
    from app.services.search_time import _zone
    from app.schemas.photo import SearchQuery

    factory, _, user = infra
    start = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
    end = datetime(2026, 9, 5, 16, tzinfo=timezone.utc)
    included = []
    for instant in [start, start, start - timedelta(microseconds=1), end]:
        pid = await add_photo(
            factory, user, status="done", taken_at=instant, embedding=[0.01] * 1024
        )
        if instant == start:
            included.append(pid)
    from app.services import search_engine

    for module in [search_engine]:
        monkeypatch.setattr(
            module,
            "get_query_embedding",
            AsyncMock(return_value=([0.01] * 1024, False)),
        )
    token = _zone.set("Asia/Shanghai")
    try:
        async with factory() as db:
            payload = SearchQuery(
                q="cat",
                from_date=date(2026, 9, 5),
                to_date=date(2026, 9, 5),
                verify_semantic=False,
                verify_constraints=False,
                w_semantic=1,
                w_recency=0,
                w_interaction=0,
            )
            http = await http_search.semantic_search(
                payload, SimpleNamespace(id=user), db
            )
            tool = await agent_tools.search_photos(
                db=db,
                user_id=user,
                query="cat",
                from_date=date(2026, 9, 5),
                to_date=date(2026, 9, 5),
                auto_parse=False,
                verify_semantic=False,
                verify_constraints=False,
                w_semantic=1,
                w_recency=0,
                w_interaction=0,
            )
            assert [str(item.id) for item in http.items] == sorted(included)
            assert [item["id"] for item in tool["items"]] == sorted(included)
    finally:
        _zone.reset(token)
