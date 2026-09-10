"""LLM decision adapter used by the PhotoAgent orchestrator."""

from __future__ import annotations

import httpx

from app.config import settings
from app.core.logger import get_logger
from app.core.telemetry import set_current_span_attributes, traced_async
from app.services.circuit_breaker import agent_llm_breaker

logger = get_logger(__name__)
_CHAT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def _is_mock_llm() -> bool:
    return not settings.dashscope_api_key or settings.dashscope_api_key.strip() in (
        "",
        "sk-xxx",
        "please_set_dashscope_key",
    )


@traced_async(
    "chat qwen-plus",
    kind="client",
    attributes={
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "alibaba_cloud",
    },
)
async def _llm_decide(
    messages: list[dict],
    tools: list[dict],
) -> tuple[dict, dict]:
    """调用 LLM 获取下一步决策（function calling 格式）。

    返回 (decision_message, usage_info)。
    usage_info 包含 total_tokens 等指标，用于预算追踪。
    """
    if _is_mock_llm():
        return (
            {
                "role": "assistant",
                "content": "当前是 mock 模式，未启用 LLM 决策。",
                "tool_calls": [],
            },
            {"total_tokens": 0},
        )

    async def _do_call() -> tuple[dict, dict]:
        payload = {
            "model": settings.qwen_chat_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": 800,
            "temperature": 0.3,
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT, trust_env=False) as client:
            resp = await client.post(
                settings.dashscope_chat_url,
                json=payload,
                headers=headers,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"Agent LLM HTTP {resp.status_code}")

        # 响应JSON解析容错
        try:
            data = resp.json()
        except Exception:
            # JSON解析失败，尝试从文本中提取
            logger.warning("LLM response json parse failed")
            raise RuntimeError("Agent LLM invalid JSON response")

        try:
            choices = data.get("choices", [])
            if not choices:
                # 无choices时，检查是否有error字段
                error = data.get("error", {})
                if error:
                    raise RuntimeError("Agent LLM API error")
                # content为空时，尝试取reasoning_content兜底
                logger.warning("LLM returned empty choices, returning empty message")
                return {"role": "assistant", "content": "", "tool_calls": []}, {
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }

            choice = choices[0]
            message = choice.get("message", {})

            # Provider reasoning is never a public answer or conversation content.
            message = {
                "role": "assistant",
                "content": message.get("content") if isinstance(message.get("content"), str) else "",
                "tool_calls": message.get("tool_calls"),
            }

            # tool_calls字段容错：确保是列表
            if "tool_calls" not in message or message["tool_calls"] is None:
                message["tool_calls"] = []

            usage = data.get("usage", {})
            return message, {
                "total_tokens": usage.get("total_tokens", 0),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Agent LLM unexpected response"
            ) from exc

    decision, usage = await agent_llm_breaker.call(_do_call)
    set_current_span_attributes(
        {
            "gen_ai.request.model": settings.qwen_chat_model,
            "gen_ai.usage.total_tokens": int(usage.get("total_tokens", 0) or 0),
            "gen_ai.usage.input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "gen_ai.usage.output_tokens": int(usage.get("completion_tokens", 0) or 0),
        }
    )
    return decision, usage
