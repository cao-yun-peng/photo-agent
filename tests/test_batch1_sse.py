import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api import agent
from app.schemas.agent import AgentRunRequest
from app.services.lock import AgentLock


@pytest.mark.asyncio
async def test_sse_disconnect_cancels_producer_even_when_queue_full(monkeypatch):
    finished = asyncio.Event()

    async def run(user_id, payload, queue):
        try:
            while True:
                await queue.put({"type": "think", "payload": {}})
        finally:
            finished.set()

    monkeypatch.setattr(agent, "_run_agent_with_lock", run)
    response = await agent.agent_stream(
        AgentRunRequest(query="cat"), SimpleNamespace(id=uuid4())
    )
    stream = response.body_iterator
    assert b"think" in (await stream.__anext__()).encode()
    await stream.aclose()
    await asyncio.wait_for(finished.wait(), 1)
    assert not any(
        t.get_name() == "agent-stream-runner" and not t.done()
        for t in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_sse_unconsumed_response_does_not_start_task(monkeypatch):
    async def run(*args):
        raise AssertionError("must not start")

    monkeypatch.setattr(agent, "_run_agent_with_lock", run)
    await agent.agent_stream(AgentRunRequest(query="cat"), SimpleNamespace(id=uuid4()))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_sse_unexpected_runner_cancel_does_not_hang(monkeypatch):
    async def run(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent, "_run_agent_with_lock", run)
    response = await agent.agent_stream(
        AgentRunRequest(query="cat"), SimpleNamespace(id=uuid4())
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(response.body_iterator.__anext__(), 1)


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.asyncio
async def test_renewal_failure_notifies_owner(monkeypatch, raises):
    lock = AgentLock(str(uuid4()))

    async def extend():
        if raises:
            raise ConnectionError()
        return False

    monkeypatch.setattr(lock, "extend", extend)
    import time

    lock._valid_until = time.monotonic() + 30
    renewal = await lock.start_auto_renew(interval=0.001)
    await asyncio.wait_for(lock.lost.wait(), 1)
    await renewal


@pytest.mark.asyncio
async def test_cleanup_deadline_survives_cancellation_suppression(monkeypatch):
    from app.services import task_lifecycle

    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(task_lifecycle.settings, "task_cleanup_timeout_seconds", 0.01)
    task = asyncio.create_task(stubborn())
    await entered.wait()
    try:
        await asyncio.wait_for(task_lifecycle.cancel_and_wait(task), 0.5)
        assert task in task_lifecycle._draining_tasks
    finally:
        release.set()
        await asyncio.wait_for(task, 1)
    await asyncio.sleep(0)
    assert task not in task_lifecycle._draining_tasks
