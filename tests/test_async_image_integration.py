"""Isolated SQL races and restart recovery; no paid image calls."""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.models.generation import Generation
from app.services import async_image
from app.services.generation_lifecycle import recover_generations
from app.services.image_gen import GenResult
from app.workers import package_tasks
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_generation_lifecycle_integration import confirmed
from tests.test_package_execution import png

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated database required",
    ),
]


@pytest.mark.asyncio
async def test_async_submit_claim_poll_restart_and_settle(infra, monkeypatch, tmp_path):  # noqa: F811
    factory, _, user = infra
    monkeypatch.setattr(settings, "openai_base_url", "https://timicc.com/v1")
    monkeypatch.setattr(settings, "openai_image_transport", "timicc_async")
    gid, _ = await confirmed(factory, user, monkeypatch, tmp_path)
    monkeypatch.setattr(package_tasks.image_gen, "_is_openai_mock", lambda: False)
    entered, release = asyncio.Event(), asyncio.Event()
    task = {"id": "imgtask_fake", "deadline": time.time() + 1000}

    async def submit(*args):
        entered.set()
        await release.wait()
        return task

    submit_mock = AsyncMock(side_effect=submit)
    monkeypatch.setattr(async_image, "submit", submit_mock)
    first = asyncio.create_task(package_tasks.run_package(factory, str(gid)))
    await asyncio.wait_for(entered.wait(), 10)
    assert not (await package_tasks.run_package(factory, str(gid)))["ok"]
    release.set()
    await first
    assert submit_mock.await_count == 1
    from app import database
    from tests.test_batch1_integration import Queue

    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    queue = Queue()
    recovered = await package_tasks.recover_generation_jobs({"redis": queue})
    assert recovered["async_polls_enqueued"] == 1
    assert queue.jobs[0][0] == ("generate_photo", str(gid))
    monkeypatch.setattr(
        async_image,
        "poll",
        AsyncMock(side_effect=httpx.ConnectError("GET interrupted")),
    )
    await package_tasks.run_package(factory, str(gid))
    async with factory() as db:
        row = await db.get(Generation, gid)
        assert row.progress_stage == "async_waiting" and row.quota_reserved
        row.progress_stage = "async_polling"
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await recover_generations(db)
    monkeypatch.setattr(
        async_image,
        "poll",
        AsyncMock(return_value=GenResult(model="mock", image_bytes=png(), cost_yuan=0)),
    )
    result = await package_tasks.run_package(factory, str(gid))
    assert result["status"] == "done"
    async with factory() as db:
        row = await db.get(Generation, gid)
        assert row.attempt_count == 1 and not row.quota_reserved
        assert row.result_oss_key
    assert submit_mock.await_count == 1
