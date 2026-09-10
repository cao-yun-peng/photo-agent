"""Exercise failure persistence without a network, model charge or production DB."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.services import provider_ledger
from app.workers import package_tasks


class Session:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args):
        return SimpleNamespace(scalar_one=lambda: self.row)

    async def commit(self):
        pass

    def add(self, row):
        self.row = row

    async def get(self, *args):
        return self.row


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadTimeout])
async def test_ledger_retains_transport_failure_without_sensitive_message(error):
    session = Session(None)

    async def handler(request):
        raise error("secret-key private prompt https://signed-url")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with provider_ledger.call_scope(lambda: session, uuid4(), "generation"):
            with pytest.raises(error):
                await provider_ledger.recorded_post(
                    client, "https://provider.example/images/edits", model="gpt-image-2"
                )
    assert session.row.status == "outcome_unknown"
    assert session.row.details["failure"]["exception_type"] == error.__name__
    assert session.row.details["latency_ms"] >= 0
    assert "http_status" not in session.row.details
    assert "secret-key" not in str(session.row.details)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,stage,code,status",
    [
        (
            httpx.RemoteProtocolError,
            "generating",
            "provider_disconnected",
            "outcome_unknown",
        ),
        (httpx.ReadTimeout, "generating", "provider_timeout", "outcome_unknown"),
        (ValueError, "validating", "validating_failed", "failed"),
    ],
)
async def test_worker_keeps_uncertain_outcome_and_records_safe_cause(
    monkeypatch, caplog, error, stage, code, status
):
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
        updated_at=datetime.now(timezone.utc),
    )
    calls, released, consumed = [], [], []

    async def validate(*args):
        if stage == "validating":
            raise error("secret-key")
        return [b"frozen"]

    async def generate(*args, **kwargs):
        calls.append(1)
        raise error("secret-key")

    async def release(*args):
        released.append(1)

    async def consume(*args):
        consumed.append(1)

    async def event(**kwargs):
        pass

    from app.services.circuit_breaker import CircuitBreaker
    from app.services import circuit_breaker
    from app.workers import gen_tasks

    monkeypatch.setattr(circuit_breaker, "image_gen_breaker", CircuitBreaker("test"))
    monkeypatch.setattr(package_tasks, "validate_execution", validate)
    monkeypatch.setattr(package_tasks.image_gen, "generate", generate)
    monkeypatch.setattr(package_tasks, "release_reserved_quota", release)
    monkeypatch.setattr(package_tasks, "consume_reserved_quota", consume)
    monkeypatch.setattr(gen_tasks, "log_event", event)

    def factory():
        return Session(row)

    result = await package_tasks.run_package(factory, str(row.id))
    assert result["status"] == row.status == status
    assert row.last_error_code == code
    assert row.verification["execution_metrics"]["failure"]["stage"] == stage
    assert row.verification["execution_metrics"]["failed_stage_ms"] >= 0
    assert released == [1] and consumed == []
    assert "secret-key" not in str(row.verification) + row.error_message + caplog.text
    if stage == "generating":
        assert "不会自动重试" in row.error_message
        await package_tasks.run_package(factory, str(row.id))
        assert calls == [1]
    else:
        assert calls == []
