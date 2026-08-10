"""HTTP中间件: LogID全链路追踪 + 访问日志 + 客户端断开处理."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.logger import generate_log_id, get_logger, set_logging_context

logger = get_logger(__name__)


# 不需要记录访问日志的路径
_SKIP_LOG_PATHS = {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}


class LogIDMiddleware(BaseHTTPMiddleware):
    """LogID全链路追踪中间件.

    - 从header/cookie提取或生成logId
    - 通过ContextVar协程安全传递
    - 注入响应头 X-Log-ID
    - 记录访问日志(含耗时/状态码/客户端IP)
    - 捕获客户端断开(CancelledError)记录499状态
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        app_name: str = "photo-agent",
        skip_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.app_name = app_name
        self.skip_paths = skip_paths or _SKIP_LOG_PATHS

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], asyncio.coroutine]
    ) -> Response:
        # 提取或生成 LogID
        log_id = (
            request.headers.get("X-Log-ID")
            or request.headers.get("X-Request-ID")
            or request.cookies.get("logId")
            or generate_log_id()
        )

        # 提取用户ID（后续在认证中间件中可更新）
        user_id = "-"

        # 设置日志上下文
        set_logging_context(
            log_id=log_id,
            user_id=user_id,
            path=request.url.path,
            method=request.method,
            client_ip=self._get_client_ip(request),
        )

        # 注入请求状态，供后续中间件/路由使用
        request.state.log_id = log_id
        request.state.start_time = time.time()

        # 跳过健康检查等路径的日志
        should_log = request.url.path not in self.skip_paths

        if should_log:
            logger.info(
                "request started | %s %s from %s",
                request.method,
                request.url.path,
                self._get_client_ip(request),
            )

        try:
            response = await call_next(request)

            # 计算耗时
            cost_ms = (time.time() - request.state.start_time) * 1000

            # 注入 LogID 到响应头
            response.headers["X-Log-ID"] = log_id

            if should_log:
                logger.info(
                    "request completed | %s %s -> %d | %.1fms",
                    request.method,
                    request.url.path,
                    response.status_code,
                    cost_ms,
                )

            return response

        except asyncio.CancelledError:
            # 客户端断开连接 (nginx风格499)
            cost_ms = (time.time() - request.state.start_time) * 1000
            logger.warning(
                "request cancelled (client disconnect) | %s %s | 499 | %.1fms",
                request.method,
                request.url.path,
                cost_ms,
            )
            return Response(status_code=499)

        except Exception as exc:  # noqa: BLE001
            cost_ms = (time.time() - request.state.start_time) * 1000
            logger.error(
                "request failed | %s %s | 500 | %.1fms | %s",
                request.method,
                request.url.path,
                cost_ms,
                exc,
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "errNo": -2,
                    "errMsg": "系统内部错误",
                    "data": None,
                },
                headers={"X-Log-ID": log_id},
            )

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """获取客户端真实IP，支持反向代理."""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        if request.client:
            return request.client.host
        return "-"
