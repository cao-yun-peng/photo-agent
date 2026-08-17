"""外部服务熔断器：为 DashScope / OSS / 微信等外部依赖提供降级保护。

三态流转：
  closed   — 正常状态，请求透传
  open     — 熔断状态，直接拒绝（抛 ServiceDegradedError）
  half_open — 探测状态，允许一次请求通过试探恢复

closed → open：连续失败 >= failure_threshold 次
open → half_open：距上次失败超过 recovery_interval 秒
half_open → closed：探测成功
half_open → open：探测失败（立即重置计时器）
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from app.config import settings

logger = logging.getLogger(__name__)


class ServiceDegradedError(Exception):
    """外部服务降级时抛出，API/Worker 捕获后返回友好提示或降级处理。"""

    def __init__(self, service_name: str, message: str = ""):
        self.service_name = service_name
        super().__init__(
            f"{service_name} is degraded" + (f": {message}" if message else "")
        )


class CircuitBreaker:
    """外部服务熔断器，包装异步调用。"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_interval: int = 300,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_interval = recovery_interval  # 秒
        self.failure_count = 0
        self.state = "closed"  # closed | open | half_open
        self.last_failure_time: float = 0.0
        self._half_open_probe_in_flight = False

    async def call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """包装一次外部调用。熔断时抛 ServiceDegradedError。"""
        # 1. 熔断状态检查
        if self.state == "open":
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed > self.recovery_interval:
                self.state = "half_open"
                logger.info("circuit_breaker %s: open → half_open (probe)", self.name)
            else:
                raise ServiceDegradedError(
                    self.name,
                    f"retry after {self.recovery_interval - elapsed:.0f}s",
                )

        # half-open 只允许一个真实探测请求，其他调用立即降级。
        if self.state == "half_open":
            if self._half_open_probe_in_flight:
                raise ServiceDegradedError(self.name, "recovery probe is in progress")
            self._half_open_probe_in_flight = True

        # 2. 执行调用（closed 和 half_open 都走这里）
        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        """调用成功：重置一切。half_open 探测成功 → 恢复 closed。"""
        if self.state == "half_open":
            logger.info("circuit_breaker %s: half_open → closed (recovered)", self.name)
        self.failure_count = 0
        self.state = "closed"
        self._half_open_probe_in_flight = False

    def _on_failure(self) -> None:
        """调用失败：累计计数。half_open 探测失败 → 立即回 open。"""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()

        if self.state == "half_open":
            self.state = "open"
            self._half_open_probe_in_flight = False
            logger.warning(
                "circuit_breaker %s: half_open → open (probe failed)", self.name
            )
        elif self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "circuit_breaker %s: closed → open (failures=%d)",
                self.name,
                self.failure_count,
            )

    def reset(self) -> None:
        """清空运行状态；主要用于相互隔离的评测用例和运维恢复。"""
        self.failure_count = 0
        self.state = "closed"
        self.last_failure_time = 0.0
        self._half_open_probe_in_flight = False

    def retry_after_seconds(self) -> int:
        """熔断时距离下一次恢复探测的秒数；closed 返回 0。"""
        if self.state == "half_open":
            return 1
        if self.state != "open":
            return 0
        elapsed = time.monotonic() - self.last_failure_time
        return max(1, math.ceil(self.recovery_interval - elapsed))

    def to_dict(self) -> dict[str, Any]:
        """返回当前状态，供健康检查使用。"""
        return {
            "name": self.name,
            "state": self.state,
            "failure_count": self.failure_count,
            "last_failure_time": self.last_failure_time,
            "recovery_interval": self.recovery_interval,
            "failure_threshold": self.failure_threshold,
        }


# 全局熔断器实例（参数从 settings 读取，支持 .env 配置）
vl_breaker = CircuitBreaker(
    "dashscope_vl",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_vl_recovery_interval,
)
embedding_breaker = CircuitBreaker(
    "dashscope_embedding",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_embedding_recovery_interval,
)
agent_llm_breaker = CircuitBreaker(
    "dashscope_chat",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_chat_recovery_interval,
)
search_rerank_breaker = CircuitBreaker(
    "dashscope_search_rerank",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_search_rerank_recovery_interval,
)
search_visual_verify_breaker = CircuitBreaker(
    "dashscope_search_visual_verify",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_search_visual_verify_recovery_interval,
)
image_gen_breaker = CircuitBreaker(
    "dashscope_image_gen",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_image_gen_recovery_interval,
)
oss_breaker = CircuitBreaker(
    "aliyun_oss",
    failure_threshold=settings.cb_failure_threshold,
    recovery_interval=settings.cb_oss_recovery_interval,
)
