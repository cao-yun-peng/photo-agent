"""OpenTelemetry 初始化与跨进程 Trace 上下文传递。

所有入口均允许在未安装 OTel 或 OTEL_ENABLED=false 时安全退化为 no-op。
业务代码只记录元数据和数量，默认不采集用户问题、提示词、模型回复或图片内容。
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, Mapping, ParamSpec

from app.config import settings
from app.core.logger import (
    JSONFormatter,
    generate_log_id,
    get_log_context,
    reset_logging_context,
    set_logging_context,
)

P = ParamSpec("P")
_initialized = False
_instrumented_fastapi_apps: set[int] = set()
_tracer_provider: Any = None
_logger_provider: Any = None


class _OtelInternalLogFilter(logging.Filter):
    """避免导出器自身报错再次进入同一个 OTLP 日志管道。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(("opentelemetry.", "urllib3."))


def _httpx_request_hook(span: Any, request_info: Any) -> None:
    """保留目标路径但移除 OSS 签名等 URL 查询参数。"""
    if span is None or not span.is_recording():
        return
    url = str(getattr(request_info, "url", ""))
    if url:
        safe_url = url.split("?", 1)[0]
        span.set_attribute("url.full", safe_url)
        span.set_attribute("http.url", safe_url)


def _redis_request_hook(
    span: Any,
    _: Any,
    args: tuple[Any, ...],
    __: Mapping[str, Any],
) -> None:
    """Redis span 只保留命令名，不导出 ARQ payload、查询和缓存正文。"""
    if span is None or not span.is_recording():
        return
    command = str(args[0]).split(maxsplit=1)[0].upper() if args else "REDIS"
    span.set_attribute("db.statement", command)


def _otlp_signal_endpoint(signal: str) -> str:
    base = settings.otel_exporter_otlp_endpoint.rstrip("/")
    suffix = f"/v1/{signal}"
    return base if base.endswith(suffix) else f"{base}{suffix}"


def _otel_available() -> bool:
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        return False
    return True


def setup_telemetry(*, service_name: str | None = None, engine: Any = None) -> bool:
    """初始化 OTLP Trace/Log 导出与常用库自动埋点。"""
    global _initialized, _tracer_provider, _logger_provider
    if _initialized:
        return True
    if not settings.otel_enabled:
        return False
    if not _otel_available():
        logging.getLogger(__name__).warning(
            "OpenTelemetry enabled but dependencies are unavailable"
        )
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        resource = Resource.create(
            {
                "service.name": service_name
                or settings.otel_service_name
                or settings.app_name,
                "deployment.environment.name": settings.app_env,
            }
        )
        ratio = min(1.0, max(0.0, float(settings.otel_trace_sample_ratio)))
        provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(ratio)),
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=_otlp_signal_endpoint("traces"))
            )
        )
        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        HTTPXClientInstrumentor().instrument(request_hook=_httpx_request_hook)
        RedisInstrumentor().instrument(request_hook=_redis_request_hook)
        if engine is not None:
            sync_engine = getattr(engine, "sync_engine", engine)
            SQLAlchemyInstrumentor().instrument(engine=sync_engine)

        if settings.otel_export_logs:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter,
            )
            from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

            log_provider = LoggerProvider(resource=resource)
            log_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(endpoint=_otlp_signal_endpoint("logs"))
                )
            )
            otel_handler = LoggingHandler(
                level=logging.NOTSET,
                logger_provider=log_provider,
            )
            otel_handler.setFormatter(JSONFormatter(settings.app_name))
            otel_handler.addFilter(_OtelInternalLogFilter())
            logging.getLogger().addHandler(otel_handler)
            _logger_provider = log_provider

        _initialized = True
        logging.getLogger(__name__).info(
            "OpenTelemetry initialized | service=%s endpoint=%s ratio=%.3f",
            service_name or settings.otel_service_name or settings.app_name,
            settings.otel_exporter_otlp_endpoint,
            ratio,
        )
        return True
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("OpenTelemetry initialization failed")
        return False


def instrument_fastapi_app(app: Any) -> bool:
    """为 FastAPI 注册服务端 span；重复调用安全。"""
    if not settings.otel_enabled or id(app) in _instrumented_fastapi_apps:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=settings.otel_excluded_urls,
        )
        _instrumented_fastapi_apps.add(id(app))
        return True
    except ImportError:
        return False
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("FastAPI instrumentation failed")
        return False


def shutdown_telemetry() -> None:
    """尽量刷新批量导出器；关闭失败不影响进程退出。"""
    for provider in (_logger_provider, _tracer_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "OpenTelemetry provider shutdown failed", exc_info=True
            )


def current_trace_ids() -> tuple[str, str]:
    """返回固定宽度 trace/span id；无有效 span 时返回 '-'。"""
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if not context.is_valid:
            return "-", "-"
        return f"{context.trace_id:032x}", f"{context.span_id:016x}"
    except (ImportError, AttributeError):
        return "-", "-"


def inject_trace_context() -> dict[str, str]:
    """生成可序列化的 W3C carrier，供 ARQ 等异步边界传递。"""
    carrier: dict[str, str] = {}
    try:
        from opentelemetry import propagate

        propagate.inject(carrier)
    except ImportError:
        pass
    log_id = str(get_log_context().get("logId") or "-")
    if log_id != "-":
        carrier["x-log-id"] = log_id
    return carrier


def _span_kind(kind: str) -> Any:
    try:
        from opentelemetry.trace import SpanKind

        return {
            "server": SpanKind.SERVER,
            "client": SpanKind.CLIENT,
            "producer": SpanKind.PRODUCER,
            "consumer": SpanKind.CONSUMER,
        }.get(kind, SpanKind.INTERNAL)
    except ImportError:
        return None


@contextmanager
def start_span(
    name: str,
    *,
    kind: str = "internal",
    attributes: Mapping[str, Any] | None = None,
    parent_carrier: Mapping[str, str] | None = None,
    link_carriers: list[Mapping[str, str]] | None = None,
) -> Iterator[Any]:
    """启动 span；支持远程父上下文及跨异步流程 Span Link。"""
    try:
        from opentelemetry import propagate, trace
        from opentelemetry.trace import Link
    except ImportError:
        yield None
        return

    parent_context = (
        propagate.extract(dict(parent_carrier)) if parent_carrier else None
    )
    links = []
    for carrier in link_carriers or []:
        linked_context = propagate.extract(dict(carrier))
        linked_span = trace.get_current_span(linked_context).get_span_context()
        if linked_span.is_valid:
            links.append(Link(linked_span))
    tracer = trace.get_tracer("photo-agent")
    with tracer.start_as_current_span(
        name,
        context=parent_context,
        kind=_span_kind(kind),
        attributes=dict(attributes or {}),
        links=links,
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


def set_current_span_attributes(attributes: Mapping[str, Any]) -> None:
    """为当前 span 设置低基数/已脱敏属性。"""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
    except (ImportError, AttributeError, TypeError):
        return


def hash_identifier(value: Any) -> str:
    """稳定散列业务标识；用于 trace 属性，避免把用户主键发往观测系统。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def traced_async(
    span_name: str,
    *,
    kind: str = "internal",
    attributes: Mapping[str, Any] | None = None,
) -> Callable[[Callable[P, Any]], Callable[P, Any]]:
    """异步函数通用埋点装饰器。"""

    def decorator(func: Callable[P, Any]) -> Callable[P, Any]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            with start_span(span_name, kind=kind, attributes=attributes):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


async def enqueue_job_with_trace(
    redis: Any,
    function: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """创建 PRODUCER span，并把上下文放入 ARQ job kwargs。"""
    with start_span(
        f"arq publish {function}",
        kind="producer",
        attributes={"messaging.system": "redis", "messaging.operation.name": "publish"},
    ):
        kwargs["trace_context"] = inject_trace_context()
        return await redis.enqueue_job(function, *args, **kwargs)


def traced_job(func: Callable[P, Any]) -> Callable[P, Any]:
    """把 ARQ job 还原为父 trace 的 CONSUMER span。"""

    @wraps(func)
    async def wrapper(
        ctx: dict[str, Any],
        *args: P.args,
        trace_context: Mapping[str, str] | None = None,
        **kwargs: P.kwargs,
    ) -> Any:
        carrier = dict(trace_context or {})
        context_token = set_logging_context(
            log_id=carrier.get("x-log-id") or generate_log_id(),
            path=f"arq:{func.__name__}",
            job_id=str(ctx.get("job_id") or "-"),
            job_name=func.__name__,
        )
        try:
            with start_span(
                f"arq process {func.__name__}",
                kind="consumer",
                parent_carrier=carrier,
                attributes={
                    "messaging.system": "redis",
                    "messaging.operation.name": "process",
                    "messaging.destination.name": func.__name__,
                    "messaging.message.id": str(ctx.get("job_id") or "-"),
                },
            ):
                result = await func(ctx, *args, **kwargs)
                if isinstance(result, Mapping):
                    set_current_span_attributes(
                        {
                            "job.ok": bool(result.get("ok", True)),
                            "job.status": str(result.get("status", "unknown")),
                        }
                    )
                return result
        finally:
            reset_logging_context(context_token)

    return wrapper
