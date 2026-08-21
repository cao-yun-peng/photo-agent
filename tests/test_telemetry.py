from __future__ import annotations

import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import logger as logger_module
from app.core.logger import (
    JSONFormatter,
    get_log_context,
    reset_logging_context,
    set_logging_context,
)
from app.core.telemetry import enqueue_job_with_trace, traced_job


class _FakeRedis:
    def __init__(self) -> None:
        self.call = None

    async def enqueue_job(self, function, *args, **kwargs):
        self.call = (function, args, kwargs)
        return object()


@pytest.mark.asyncio
async def test_enqueue_job_propagates_log_id_and_trace_carrier() -> None:
    redis = _FakeRedis()
    token = set_logging_context(log_id="request-log-123", path="/photos")
    try:
        await enqueue_job_with_trace(redis, "process_photo", "photo-1", _job_id="job-1")
    finally:
        reset_logging_context(token)

    assert redis.call is not None
    function, args, kwargs = redis.call
    assert function == "process_photo"
    assert args == ("photo-1",)
    assert kwargs["_job_id"] == "job-1"
    assert kwargs["trace_context"]["x-log-id"] == "request-log-123"


@pytest.mark.asyncio
async def test_traced_job_restores_context_after_worker_call() -> None:
    seen = {}

    async def task(ctx, value):
        seen.update(get_log_context())
        return {"ok": True, "status": value}

    wrapped = traced_job(task)
    token = set_logging_context(log_id="outer", path="test")
    try:
        result = await wrapped(
            {"job_id": "job-7"},
            "done",
            trace_context={"x-log-id": "parent-log"},
        )
        assert get_log_context()["logId"] == "outer"
    finally:
        reset_logging_context(token)

    assert result == {"ok": True, "status": "done"}
    assert seen["logId"] == "parent-log"
    assert seen["job_id"] == "job-7"
    assert seen["job_name"] == "task"


@pytest.mark.asyncio
async def test_arq_consumer_is_child_of_producer_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    redis = _FakeRedis()

    async def task(ctx, value):
        return {"ok": True, "status": value}

    with trace.get_tracer("test").start_as_current_span("http request"):
        await enqueue_job_with_trace(redis, "task", "done")

    assert redis.call is not None
    trace_context = redis.call[2]["trace_context"]
    await traced_job(task)({"job_id": "job-8"}, "done", trace_context=trace_context)

    spans = {span.name: span for span in exporter.get_finished_spans()}
    producer = spans["arq publish task"]
    consumer = spans["arq process task"]
    assert consumer.context.trace_id == producer.context.trace_id
    assert consumer.parent is not None
    assert consumer.parent.span_id == producer.context.span_id


def test_json_log_contains_trace_and_span_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        logger_module,
        "_current_trace_ids",
        lambda: ("1" * 32, "2" * 16),
    )
    record = logging.LogRecord(
        name="test.telemetry",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JSONFormatter().format(record))

    assert payload["trace_id"] == "1" * 32
    assert payload["span_id"] == "2" * 16
    assert payload["userIdHash"] == "-"
    assert payload["msg"] == "hello"
