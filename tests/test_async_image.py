import base64
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.services import async_image
from app.services.image_gen import GenResult
from app.services.provider_contract import image_contract
from app.workers import package_tasks, gen_tasks
from tests.test_generation_failure_diagnostics import Session
from tests.test_package_execution import png


@pytest.mark.asyncio
async def test_async_submit_and_poll_use_distinct_routes_and_never_resubmit(
    monkeypatch,
):
    monkeypatch.setattr(settings, "openai_base_url", "https://timicc.com/v1")
    requests = []
    responses = [
        httpx.Response(202, json={"task_id": "imgtask_test"}),
        httpx.Response(200, json={"status": "processing"}),
        httpx.Response(
            200,
            json={
                "status": "completed",
                "result": {"data": [{"b64_json": base64.b64encode(png()).decode()}]},
            },
        ),
    ]

    def handler(request):
        requests.append(request)
        return responses.pop(0)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        async_image.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )
    task = await async_image.submit("knit", [png(), png()], "1024x1024")
    assert await async_image.poll(task, None, None, None) is None
    result = await async_image.poll(task, None, None, None)
    assert result.image_bytes == png()
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    assert requests[0].url.path == "/v1/images/edits/async"
    assert requests[0].content.count(b'name="image[]"') == 2
    assert requests[1].url.path == "/v1/images/tasks/imgtask_test"


@pytest.mark.parametrize(
    "url", ["https://other.example/v1", "https://timicc.com/other"]
)
def test_async_transport_is_explicit_and_provider_specific(url):
    with pytest.raises(ValueError):
        image_contract(url, "timicc_async")
    assert "transport" not in image_contract(url)


@pytest.mark.asyncio
async def test_provider_rate_limit_has_explicit_code(monkeypatch):
    monkeypatch.setattr(settings, "openai_base_url", "https://timicc.com/v1")
    client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"status": "failed", "error": {"type": "rate_limit_error"}}
        )
    )
    monkeypatch.setattr(
        async_image.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=transport, **kwargs),
    )
    with pytest.raises(async_image.AsyncImageFailed) as failure:
        await async_image.poll(
            {"id": "imgtask_test", "base_url": "https://timicc.com/v1"},
            None,
            None,
            None,
        )
    assert failure.value.code == "provider_rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_worker_resumes_get_after_network_failure_without_second_post(
    monkeypatch, cancelled
):
    monkeypatch.setattr(settings, "openai_image_transport", "timicc_async")
    monkeypatch.setattr(package_tasks.image_gen, "_is_openai_mock", lambda: False)
    row = SimpleNamespace(
        id=uuid4(),
        status="pending",
        attempt_count=0,
        execution_snapshot={"prompt": "private", "size": "1024x1024"},
        user_id=uuid4(),
        skill_id=None,
        model="gpt-image-2",
        verification=None,
        cost_yuan=Decimal("0"),
        iteration_index=0,
    )
    task = {"id": "imgtask_test", "deadline": time.time() + 1000}
    submit = AsyncMock(return_value=task)
    poll = AsyncMock(
        side_effect=[
            httpx.ConnectError("transient"),
            None,
            GenResult(model="gpt-image-2", image_bytes=png(), cost_yuan=0.3),
        ]
    )
    monkeypatch.setattr(async_image, "submit", submit)
    monkeypatch.setattr(async_image, "poll", poll)
    monkeypatch.setattr(
        package_tasks, "validate_execution", AsyncMock(return_value=[png()])
    )
    monkeypatch.setattr(
        package_tasks, "review_output", AsyncMock(return_value={"status": "passed"})
    )
    put = AsyncMock()
    monkeypatch.setattr(package_tasks.oss, "put_object", put)
    consume, release = AsyncMock(), AsyncMock()
    monkeypatch.setattr(package_tasks, "consume_reserved_quota", consume)
    monkeypatch.setattr(package_tasks, "release_reserved_quota", release)
    monkeypatch.setattr(gen_tasks, "log_event", AsyncMock())

    def factory():
        return Session(row)

    await package_tasks.run_package(factory, str(row.id))
    assert row.progress_stage == "async_waiting" and row.lease_token is None
    if cancelled:
        row.status = "cancel_requested"
    await package_tasks.run_package(factory, str(row.id))
    assert row.progress_stage == "async_waiting"
    await package_tasks.run_package(factory, str(row.id))
    assert row.progress_stage == "async_waiting"
    await package_tasks.run_package(factory, str(row.id))
    assert row.status == ("cancelled" if cancelled else "done")
    await package_tasks.run_package(factory, str(row.id))
    assert submit.await_count == 1 and poll.await_count == 3
    assert row.attempt_count == 1
    assert consume.await_count == 1 and release.await_count == 0
    assert put.await_count == (0 if cancelled else 1)


@pytest.mark.asyncio
async def test_async_recovery_never_marks_get_interruption_as_final_failure(
    monkeypatch,
):
    from app.services.generation_lifecycle import recover_generations

    row = SimpleNamespace(
        progress_stage="async_polling",
        verification={"provider_task": {"id": "imgtask_test"}},
        lease_token=uuid4(),
        lease_expires_at=datetime.now(timezone.utc),
        status="processing",
    )

    class RecoverySession(Session):
        async def execute(self, *args):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    assert await recover_generations(RecoverySession(row)) == 0
    assert row.status == "processing" and row.progress_stage == "async_waiting"
    assert row.lease_token is None
