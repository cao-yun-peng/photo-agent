"""监控指标采集：对接 Prometheus / 日志平台。

设计要点：
- 不强制依赖 prometheus_client，优先用日志 + 结构化输出；
- 如果环境安装了 prometheus_client，则自动暴露 Counter/Histogram；
- 所有指标方法都提供 tags 支持，便于后续按服务/用户维度拆分。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# 可选的 prometheus_client；未安装时退化为结构化日志
try:
    from prometheus_client import Counter, Histogram

    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False


class _NoopMetric:
    """prometheus_client 未安装时的空实现。"""

    def inc(self, amount: float = 1) -> None:
        pass

    def observe(self, value: float) -> None:
        pass


class AgentMetrics:
    """Agent 与照片处理相关的监控指标。

    使用方式：
        metrics = AgentMetrics()
        metrics.record_session_end(state_dict, elapsed=1.2, tokens=1200)
        metrics.record_vl_parse_failure(level="fallback")
        metrics.record_photo_status(status="done")
    """

    def __init__(self) -> None:
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}

    def _counter(self, name: str, description: str, labels: list[str]) -> Any:
        if name not in self._counters:
            if _PROMETHEUS_AVAILABLE:
                self._counters[name] = Counter(name, description, labels)
            else:
                self._counters[name] = _NoopMetric()
        return self._counters[name]

    def _histogram(self, name: str, description: str, labels: list[str]) -> Any:
        if name not in self._histograms:
            if _PROMETHEUS_AVAILABLE:
                self._histograms[name] = Histogram(
                    name, description, labels, buckets=(0.5, 1, 2, 3, 5, 10, 20, 60)
                )
            else:
                self._histograms[name] = _NoopMetric()
        return self._histograms[name]

    def _log(self, metric_type: str, name: str, value: float, tags: dict[str, str]) -> None:
        """除了 Prometheus，还写一条结构化日志，便于没有 Prometheus 时排查。"""
        logger.info(
            "metric | type=%s name=%s value=%s tags=%s",
            metric_type,
            name,
            value,
            tags,
        )

    def counter(self, name: str, tags: dict[str, str] | None = None) -> None:
        """记录一个计数器指标。"""
        tags = tags or {}
        if settings.app_env != "test":
            self._log("counter", name, 1.0, tags)
        if _PROMETHEUS_AVAILABLE:
            metric = self._counter(name, f"Counter: {name}", list(tags.keys()))
            metric.labels(**tags).inc(1)

    def histogram(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """记录一个直方图指标。"""
        tags = tags or {}
        if settings.app_env != "test":
            self._log("histogram", name, value, tags)
        if _PROMETHEUS_AVAILABLE:
            metric = self._histogram(name, f"Histogram: {name}", list(tags.keys()))
            metric.labels(**tags).observe(value)

    def record_session_end(
        self,
        state: dict[str, Any],
        elapsed: float,
        tokens: int = 0,
    ) -> None:
        """每次 Agent 会话结束时调用。"""
        attempts = state.get("attempts") or state.get("step", 0)
        self.histogram("agent_iterations", float(attempts))
        self.histogram("agent_latency_seconds", elapsed)
        self.histogram("agent_tokens_total", float(tokens))

        if state.get("fallback_triggered"):
            self.counter(
                "agent_fallback_total",
                tags={"strategy": state.get("current_strategy", "unknown")},
            )

    def record_vl_parse_failure(self, level: str) -> None:
        """VL 输出解析失败时调用。level 如 ok/fallback/empty_response/..."""
        if level != "ok":
            self.counter("vl_parse_failure_total", tags={"level": level})

    def record_photo_status(self, status: str) -> None:
        """照片处理完成时调用。"""
        self.counter("photo_process_total", tags={"status": status})

    def record_tool_call(self, tool_name: str, success: bool) -> None:
        """Tool 调用结束时调用。"""
        self.counter(
            "agent_tool_call_total",
            tags={"tool": tool_name, "status": "ok" if success else "error"},
        )

    def timeit(self, name: str, tags: dict[str, str] | None = None):
        """上下文管理器，用于测量一段代码的耗时。

        async with metrics.timeit("vl_call"):
            result = await describe_image(url)
        """
        return _MetricsTimer(self, name, tags)


class _MetricsTimer:
    def __init__(
        self,
        metrics: AgentMetrics,
        name: str,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.metrics = metrics
        self.name = name
        self.tags = tags or {}
        self.start: float = 0.0

    async def __aenter__(self) -> "_MetricsTimer":
        self.start = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        elapsed = time.monotonic() - self.start
        self.metrics.histogram(f"{self.name}_seconds", elapsed, self.tags)


# 全局默认实例，便于随处导入使用
metrics = AgentMetrics()
