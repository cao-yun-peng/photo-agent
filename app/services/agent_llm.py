"""LLM decision adapter used by the PhotoAgent orchestrator."""

from __future__ import annotations

import json

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
        # mock 模式下返回一个安全的默认决策
        return (
            {
                "role": "assistant",
                "content": "mock 模式：直接给出最终答案。",
                "tool_calls": [
                    {
                        "id": "mock-call-1",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps(
                                {
                                    "message": "当前是 mock 模式，未启用 LLM 决策。"
                                    "请在 .env 中配置 DASHSCOPE_API_KEY 后重试。"
                                }
                            ),
                        },
                    }
                ],
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
            raise RuntimeError(f"Agent LLM HTTP {resp.status_code}: {resp.text[:300]}")

        # 响应JSON解析容错
        try:
            data = resp.json()
        except Exception:
            # JSON解析失败，尝试从文本中提取
            resp_text = resp.text
            logger.warning("LLM response json parse failed, raw: %s", resp_text[:500])
            raise RuntimeError(f"Agent LLM invalid JSON response: {resp_text[:300]}")

        try:
            choices = data.get("choices", [])
            if not choices:
                # 无choices时，检查是否有error字段
                error = data.get("error", {})
                if error:
                    raise RuntimeError(f"Agent LLM API error: {error}")
                # content为空时，尝试取reasoning_content兜底
                logger.warning("LLM returned empty choices, returning empty message")
                return {"role": "assistant", "content": "", "tool_calls": []}, {
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }

            choice = choices[0]
            message = choice.get("message", {})

            # content为空时，尝试取reasoning_content兜底（兼容推理模型）
            if not message.get("content") and message.get("reasoning_content"):
                logger.debug("using reasoning_content as fallback for empty content")
                message["content"] = message.get("reasoning_content", "")

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
                f"Agent LLM unexpected response: {str(data)[:500]}"
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
