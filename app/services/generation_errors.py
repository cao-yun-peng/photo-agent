"""Safe diagnostics: never persist exception messages, requests or provider bodies."""

import httpx


def failure_details(exc):
    if isinstance(exc, httpx.RemoteProtocolError):
        code, message = (
            "provider_disconnected",
            "图片服务连接被对端中断，未收到完整响应。",
        )
    elif isinstance(exc, httpx.TimeoutException):
        code, message = "provider_timeout", "等待图片服务响应超时。"
    elif isinstance(exc, httpx.RequestError):
        code, message = "provider_network_error", "图片服务网络请求未完成。"
    else:
        code, message = "generation_execution_error", "任务执行未完成。"
    return {"code": code, "exception_type": type(exc).__name__}, message
