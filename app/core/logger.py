"""JSON结构化日志 + ContextVar 全链路 LogID 传递.

参考 llm-rag-server 生产级日志设计:
- JSONFormatter: 所有日志输出为结构化JSON
- NOTICE 级别: 自定义统计级别(25)，用于关键指标采集
- ContextLogger: 自动注入logId/userId等上下文
- RotatingFileHandler: 按大小滚动日志
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

# ---------- NOTICE 自定义日志级别 ----------
NOTICE = 25
logging.addLevelName(NOTICE, "NOTICE")


def notice(self: logging.Logger, key: str, value: Any = None, **kwargs: Any) -> None:
    """NOTICE级别日志，用于可采集的统计指标.

    用法: logger.notice("llm_token_usage", {"model": "qwen-plus", "tokens": 1500})
    """
    if self.isEnabledFor(NOTICE):
        extra = kwargs.pop("extra", {})
        extra["_notice_key"] = key
        if value is not None:
            extra["_notice_value"] = value
        self._log(NOTICE, key, (), extra=extra, **kwargs)


logging.Logger.notice = notice  # type: ignore[attr-defined]


# ---------- 上下文变量 (协程安全) ----------
_log_context: ContextVar[Dict[str, Any]] = ContextVar(
    "log_context",
    default={"logId": "-", "userId": "-", "path": "-"},
)


def set_logging_context(
    log_id: Optional[str] = None,
    user_id: Optional[str] = None,
    path: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """设置当前协程的日志上下文."""
    ctx = _log_context.get().copy()
    if log_id is not None:
        ctx["logId"] = log_id
    if user_id is not None:
        ctx["userId"] = str(user_id)
    if path is not None:
        ctx["path"] = path
    ctx.update(kwargs)
    _log_context.set(ctx)


def get_log_context() -> Dict[str, Any]:
    """获取当前协程的日志上下文."""
    return _log_context.get()


def generate_log_id() -> str:
    """生成唯一 LogID."""
    return uuid.uuid4().hex[:16]


# ---------- JSON 格式化器 ----------
class JSONFormatter(logging.Formatter):
    """JSON结构化日志格式化器."""

    def __init__(self, app_name: str = "photo-agent") -> None:
        super().__init__()
        self.app_name = app_name

    def format(self, record: logging.LogRecord) -> str:
        ctx = _log_context.get()

        log_data: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "app": self.app_name,
            "logger": record.name,
            "file": f"{os.path.basename(record.pathname)}:{record.lineno}",
            "logId": ctx.get("logId", "-"),
            "userId": ctx.get("userId", "-"),
            "path": ctx.get("path", "-"),
            "msg": record.getMessage(),
        }

        # NOTICE级别特有字段
        if record.levelno == NOTICE:
            log_data["logType"] = "notice"
            if hasattr(record, "_notice_key"):
                log_data["key"] = record._notice_key
            if hasattr(record, "_notice_value"):
                log_data["value"] = record._notice_value

        # 异常信息
        if record.exc_info and record.exc_info[0] is not None:
            log_data["exc_info"] = self.formatException(record.exc_info)

        # 额外字段
        for key, value in record.__dict__.items():
            if key.startswith("_") and key not in ("_notice_key", "_notice_value"):
                log_data[key[1:]] = value

        return json.dumps(log_data, ensure_ascii=False, default=str)


# ---------- 控制台彩色格式化器 (开发环境用) ----------
class ConsoleFormatter(logging.Formatter):
    """开发环境控制台彩色格式化器."""

    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "NOTICE": "\033[35m",    # Magenta
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[41m",  # Red bg
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        ctx = _log_context.get()
        log_id = ctx.get("logId", "-")[:8]
        user_id = ctx.get("userId", "-")
        color = self.COLORS.get(record.levelname, "")

        parts = [
            f"{color}{record.levelname:<8}{self.RESET}",
            f"[{log_id}]",
            f"{record.name}:{record.lineno}",
        ]
        if user_id != "-":
            parts.append(f"(user={user_id})")
        parts.append(record.getMessage())

        if record.exc_info and record.exc_info[0] is not None:
            parts.append("\n" + self.formatException(record.exc_info))

        return " ".join(parts)


# ---------- 日志初始化 ----------
def setup_logging(
    log_level: str = "INFO",
    log_dir: Optional[str] = None,
    json_format: bool = True,
) -> None:
    """初始化日志配置.

    Args:
        log_level: 日志级别 DEBUG/INFO/WARNING/ERROR
        log_dir: 日志文件目录，None则仅输出到控制台
        json_format: 是否使用JSON格式（生产环境True，开发环境False）
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 清除已有handler
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        JSONFormatter() if json_format else ConsoleFormatter()
    )
    root_logger.addHandler(console_handler)

    # File handler (可选)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "photo-agent.log"),
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(JSONFormatter())
        root_logger.addHandler(file_handler)

    # 降低第三方库日志级别
    for noisy_logger in ("httpx", "httpcore", "uvicorn.access", "arq"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取logger实例."""
    return logging.getLogger(name)
