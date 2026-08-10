"""轻量级 Agent 核心：while 循环 + Tool 注册表 + function calling。

设计要点：
- 不引入 LangGraph 等重型框架，核心代码控制在 200 行以内；
- LLM 只做"下一步决策"，业务逻辑交给 Tool；
- 所有 Tool 调用结果写回 State，便于多轮自我反思；
- 输出是事件流（dict 列表），后续可对接 SSE。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.agent_tools import (
    _classify_exception,
    apply_skill,
    browse_candidates,
    fallback_search,
    get_photo_detail,
    recommend_skills_for_agent,
    search_photos,
)
from app.services.circuit_breaker import ServiceDegradedError, agent_llm_breaker
from app.services.query_parser import parse_query

logger = logging.getLogger(__name__)

# DashScope Chat API（兼容 OpenAI 格式，支持 function calling）
_CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_CHAT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# 默认系统 Prompt，定义 Agent 的行为边界
_DEFAULT_SYSTEM_PROMPT = """你是 Photo Agent，一个帮助用户从个人相册中找照片、改造照片的 AI 助手。

你可以使用以下工具：
1. search_photos：根据自然语言描述搜索用户相册中的照片。
2. fallback_search：当 search_photos 无结果时，按三级兜底策略查找：线索相册→时间线→全相册。
3. browse_candidates：按时间列出相册照片让用户自己挑选（最终兜底）。
4. apply_skill：对指定照片应用 AI 改造 Skill（如动漫风、老照片修复等）。
5. get_photo_detail：查看单张照片的详细信息。
6. recommend_skills：基于用户画像和当前上下文照片，主动推荐可能想用的 Skill。
7. ask_clarification：当需求模糊或搜索失败 2 次时，向用户提出澄清问题并提供 2-4 个选项。
8. final_answer：向用户给出最终回复，必须包含清晰结论。

工作原则（严格遵守）：
1. 意图判断：每次用户请求后，先判断是搜索、生成、还是其他意图。
2. **搜索优先原则**：只要用户查询中包含任何具体线索（物体名如"猫/樱花/海边"、地点、颜色、时间、特征词等），**必须先调用 search_photos 尝试搜索**，禁止直接调用 ask_clarification。
3. **澄清触发条件（严格限制）**：ask_clarification 仅在以下情况使用：
   - 用户查询完全没有任何具体关键词（例如只说"找照片"、"帮我找一下"），此时可以首次澄清；
   - 累计调用 search_photos 搜索失败 2 次后仍无结果，系统自动触发澄清；
   - 其他情况一律先搜索，不要因为"缺少时间/地点"就直接澄清，用户可能只记得物体关键词。
4. 搜索重试：若 search_photos 结果为空，可换个关键词或放宽条件重试 1 次。
5. 兜底策略：累计搜索失败 2 次后，调用 fallback_search 进行三级兜底，不要直接放弃。
6. 生成意图：当用户明确想要"改造/生成/变成XX风格"某张照片时，且上下文中已有确认的照片，才调用 apply_skill。
7. Skill 推荐：当用户找到某张照片后询问"有什么风格/滤镜可以用"时，调用 recommend_skills。
8. 每次决策前，简要说明你的思考（reasoning）。
9. 必须以 final_answer 结束对话，告知用户结果或下一步操作建议。"""


# ------------------------------------------------------------------
# 配置与状态
# ------------------------------------------------------------------
@dataclass
class AgentConstraints:
    """Agent 运行约束。"""

    max_steps: int = 8
    max_searches: int = 3
    max_clarifications: int = 2
    enable_browse_fallback: bool = True
    # P0-1: 循环预算 — 时间/Token/费用
    max_time_seconds: int = settings.agent_max_time_seconds
    max_total_tokens: int = settings.agent_max_total_tokens
    max_cost_yuan: float = settings.agent_max_cost_yuan
    # P0-2: 单工具执行超时
    tool_timeout: int = settings.agent_tool_timeout


@dataclass
class AgentState:
    """一次 Agent 任务的运行时状态。"""

    session_id: UUID
    user_id: UUID
    original_query: str

    step: int = 0
    search_attempts: int = 0
    clarification_attempts: int = 0

    # 被拒绝的照片 ID，重搜时要排除
    rejected_photo_ids: set[str] = field(default_factory=set)

    # 已确认的 photo_id / generation_id
    confirmed_photo_id: str | None = None
    confirmed_generation_id: str | None = None

    # 最近一次搜索结果
    last_search_items: list[dict] = field(default_factory=list)

    # 兜底策略已走到第几级（0=未启用，1=线索相册，2=时间线，3=全相册）
    fallback_level: int = 0

    # P1-3: 可观测性 — 累计 Token 和费用
    total_tokens: int = 0
    total_cost: float = 0.0

    # 决策历史：每一步的 reasoning + tool + result_summary
    history: list[dict] = field(default_factory=list)

    # 会话创建/过期时间
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=10)
    )

    def to_json(self) -> dict:
        return {
            "session_id": str(self.session_id),
            "user_id": str(self.user_id),
            "original_query": self.original_query,
            "step": self.step,
            "search_attempts": self.search_attempts,
            "clarification_attempts": self.clarification_attempts,
            "rejected_photo_ids": list(self.rejected_photo_ids),
            "confirmed_photo_id": self.confirmed_photo_id,
            "confirmed_generation_id": self.confirmed_generation_id,
            "last_search_items": self.last_search_items,
            "fallback_level": self.fallback_level,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "history": self.history,
        }

    @classmethod
    def from_json(cls, data: dict, session_id: UUID | None = None) -> AgentState:
        """从 DB 的 JSON 恢复运行时状态。"""
        sid = session_id or UUID(data["session_id"])
        return cls(
            session_id=sid,
            user_id=UUID(data["user_id"]),
            original_query=data.get("original_query", ""),
            step=data.get("step", 0),
            search_attempts=data.get("search_attempts", 0),
            clarification_attempts=data.get("clarification_attempts", 0),
            rejected_photo_ids=set(data.get("rejected_photo_ids", [])),
            confirmed_photo_id=data.get("confirmed_photo_id"),
            confirmed_generation_id=data.get("confirmed_generation_id"),
            last_search_items=data.get("last_search_items", []),
            fallback_level=data.get("fallback_level", 0),
            total_tokens=data.get("total_tokens", 0),
            total_cost=data.get("total_cost", 0.0),
            history=data.get("history", []),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )


# ------------------------------------------------------------------
# Tool 注册表
# ------------------------------------------------------------------
ToolFn = Callable[..., Awaitable[dict]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn
    # P0-2: 单工具超时秒数，None 表示使用默认值
    timeout: int | None = None


class ToolRegistry:
    """Agent 可调用的 Tool 注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]


async def ask_clarification(
    *,
    question: str,
    options: list[str] | None = None,
    **kwargs: Any,
) -> dict:
    """向用户发起澄清问题。Agent 遇到此结果会暂停并等待用户回复。

    **kwargs 用于吸收 Agent 循环注入的 user_id、db 等公共参数，
    ask_clarification 不需要这些参数，直接忽略。
    """
    return {
        "ok": True,
        "needs_clarification": True,
        "question": question,
        "options": options or [],
    }


async def _generate_clarification(query: str) -> dict:
    """搜索失败 2 次后，基于 query 解析自动生成澄清选项。"""
    parsed = await parse_query(query)
    options: list[str] = []

    if not parsed.place:
        options.append("你想找哪个地点的照片？")
    if not parsed.from_date and not parsed.to_date:
        options.append("大概是哪段时间的照片？")
    if not parsed.tags:
        options.append("是人物照、风景照还是其他类型？")

    # 兜底选项，确保至少有 2 个
    if len(options) < 2:
        options.extend(
            [
                "可以换种说法再描述一下吗？",
                "先列出最近的照片让我自己挑",
            ]
        )

    return await ask_clarification(
        question="抱歉没找到完全匹配的照片，能帮我缩小一下范围吗？",
        options=options[:4],
    )


# ------------------------------------------------------------------
# 默认 Tool 注册
# ------------------------------------------------------------------
def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="search_photos",
            description="根据自然语言描述搜索用户相册中的照片。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索描述，如：去年夏天在海边拍的猫",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "开始日期，格式 YYYY-MM-DD，可选",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "结束日期，格式 YYYY-MM-DD，可选",
                    },
                },
                "required": ["query"],
            },
            fn=search_photos,
        )
    )

    registry.register(
        ToolSpec(
            name="browse_candidates",
            description="当搜索找不到合适照片时，列出用户相册中的照片让用户自己挑选。",
            parameters={
                "type": "object",
                "properties": {
                    "from_date": {
                        "type": "string",
                        "description": "开始日期，格式 YYYY-MM-DD，可选",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "结束日期，格式 YYYY-MM-DD，可选",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量，默认 50",
                    },
                },
            },
            fn=browse_candidates,
        )
    )

    registry.register(
        ToolSpec(
            name="fallback_search",
            description="当普通搜索无结果时，按三级兜底策略查找照片：线索相册→时间线→全相册。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言搜索描述",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "开始日期，格式 YYYY-MM-DD，可选",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "结束日期，格式 YYYY-MM-DD，可选",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量，默认 30",
                    },
                },
                "required": ["query"],
            },
            fn=fallback_search,
        )
    )

    registry.register(
        ToolSpec(
            name="ask_clarification",
            description="当用户需求模糊、缺少关键信息时，向用户提出澄清问题并提供 2-4 个选项。",
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "要问用户的澄清问题",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 个快速选项，方便用户一键回复",
                    },
                },
                "required": ["question"],
            },
            fn=ask_clarification,
        )
    )

    registry.register(
        ToolSpec(
            name="apply_skill",
            description="对指定照片应用 AI 改造 Skill。只有用户明确想要改造时才调用。",
            parameters={
                "type": "object",
                "properties": {
                    "photo_id": {
                        "type": "string",
                        "description": "要改造的照片 ID",
                    },
                    "skill_id": {
                        "type": "string",
                        "description": "Skill ID，可选；不传则使用默认生图模型",
                    },
                    "extra_prompt": {
                        "type": "string",
                        "description": "额外补充描述，可选",
                    },
                },
                "required": ["photo_id"],
            },
            fn=apply_skill,
        )
    )

    registry.register(
        ToolSpec(
            name="get_photo_detail",
            description="获取单张照片的完整结构化信息。",
            parameters={
                "type": "object",
                "properties": {
                    "photo_id": {
                        "type": "string",
                        "description": "照片 ID",
                    },
                },
                "required": ["photo_id"],
            },
            fn=get_photo_detail,
        )
    )

    registry.register(
        ToolSpec(
            name="recommend_skills",
            description="基于用户画像和上下文照片，主动推荐可能想用的 AI 改造 Skill。",
            parameters={
                "type": "object",
                "properties": {
                    "photo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "上下文照片 ID 列表，如搜索结果中的照片",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量，默认 5",
                    },
                },
            },
            fn=recommend_skills_for_agent,
        )
    )

    # final_answer 是伪工具，实际由 _execute_tool 拦截；注册它是为了让 LLM 在工具列表里看到。
    registry.register(
        ToolSpec(
            name="final_answer",
            description="向用户给出最终回复，必须包含清晰结论。",
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "给用户的最终回复",
                    },
                },
                "required": ["message"],
            },
            fn=lambda **kwargs: {"ok": True, "message": kwargs.get("message", "")},
        )
    )

    return registry


DEFAULT_REGISTRY = _build_registry()


# ------------------------------------------------------------------
# LLM 决策层
# ------------------------------------------------------------------
def _is_mock_llm() -> bool:
    return not settings.dashscope_api_key or settings.dashscope_api_key.strip() in (
        "",
        "sk-xxx",
        "please_set_dashscope_key",
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

        async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT) as client:
            resp = await client.post(_CHAT_URL, json=payload, headers=headers)

        if resp.status_code != 200:
            raise RuntimeError(f"Agent LLM HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            choice = data["choices"][0]
            usage = data.get("usage", {})
            return choice["message"], {
                "total_tokens": usage.get("total_tokens", 0),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Agent LLM unexpected response: {data}") from exc

    return await agent_llm_breaker.call(_do_call)


# ------------------------------------------------------------------
# Agent 主循环
# ------------------------------------------------------------------
class PhotoAgent:
    """轻量级 Agent：驱动搜索/兜底/生成的决策循环。"""

    def __init__(
        self,
        db: AsyncSession,
        constraints: AgentConstraints | None = None,
        registry: ToolRegistry | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.db = db
        self.constraints = constraints or AgentConstraints()
        self.registry = registry or DEFAULT_REGISTRY
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    async def run(
        self,
        user_id: UUID,
        query: str,
        session_id: UUID | None = None,
        initial_state: AgentState | None = None,
        event_queue: asyncio.Queue | None = None,
    ) -> tuple[AgentState, list[dict]]:
        """运行一次 Agent 任务，返回最终状态和事件流。

        Args:
            user_id: 当前用户 ID（用于校验）。
            query: 用户本次输入。
            session_id: 新建会话时可选指定；续会话时由 initial_state 提供。
            initial_state: 续会话时传入的上一次状态；会覆盖 session_id 并更新 original_query。
            event_queue: 可选的异步队列；若提供，每产生一个事件会实时放入队列，便于 SSE 推送。
        """
        if initial_state is not None:
            state = initial_state
            state.original_query = query
            # 新一轮用户输入，重置步数计数，但保留上下文信息
            state.step = 0
            state.search_attempts = 0
            state.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        else:
            state = AgentState(
                session_id=session_id or uuid4(),
                user_id=user_id,
                original_query=query,
            )
        events: list[dict] = []
        start_monotonic = time.monotonic()

        def emit(event_type: str, payload: dict) -> None:
            elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)
            event = {
                "type": event_type,
                "payload": payload,
                "step": state.step,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": elapsed_ms,
            }
            events.append(event)
            if event_queue is not None:
                try:
                    event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Agent event queue is full, dropping event")

        emit("start", {"query": query, "session_id": str(state.session_id)})

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": query},
        ]

        final_message = ""
        while state.step < self.constraints.max_steps:
            state.step += 1

            # P0-1: 时间预算检查
            elapsed_seconds = time.monotonic() - start_monotonic
            if elapsed_seconds > self.constraints.max_time_seconds:
                final_message = (
                    f"处理时间已超过上限（{self.constraints.max_time_seconds}s），"
                    "请告诉我更具体的需求。"
                )
                emit("final", {"message": final_message, "reason": "time_budget"})
                break

            # P0-1: Token 预算检查
            if state.total_tokens >= self.constraints.max_total_tokens:
                final_message = (
                    f"对话上下文已达到 Token 上限（{self.constraints.max_total_tokens}），"
                    "请开启新会话或简化需求。"
                )
                emit("final", {"message": final_message, "reason": "token_budget"})
                break

            # 构造当前上下文
            messages = self._build_context(messages, state)

            # 调用 LLM 决策
            try:
                decision, usage = await _llm_decide(messages, self.registry.schemas())
            except ServiceDegradedError:
                logger.warning("Agent LLM degraded, falling back to deterministic browse")
                final_message = "AI 决策服务暂时不可用，已为你打开相册浏览。"
                browse_result = await browse_candidates(
                    user_id=user_id, db=self.db, limit=30
                )
                emit("tool_call", {"tool": "browse_candidates", "arguments": "{}"})
                emit("tool_result", {"tool": "browse_candidates", "result": browse_result})
                emit("final", {"message": final_message, "fallback": "browse_candidates"})
                break
            except Exception as exc:
                logger.exception("Agent LLM decision failed")
                final_message = f"决策服务暂时不可用：{exc}，请稍后再试。"
                emit("error", {"message": final_message})
                break

            reasoning = decision.get("content", "")
            tool_calls = decision.get("tool_calls", [])

            # P0-1/P1-3: 追踪 Token 消耗
            tokens_used = usage.get("total_tokens", 0)
            state.total_tokens += tokens_used

            emit("think", {
                "reasoning": reasoning or "（无显式思考）",
                "tokens_used": tokens_used,
                "total_tokens": state.total_tokens,
            })
            state.history.append(
                {
                    "step": state.step,
                    "reasoning": reasoning,
                    "tool_calls": tool_calls,
                }
            )

            # 终止条件：LLM 没有 tool_calls，直接给出最终答案
            if not tool_calls:
                final_message = reasoning or "我已尽力处理，但没有进一步操作。"
                emit("final", {"message": final_message})
                break

            # 执行 tool_calls
            for tc in tool_calls:
                tool_name = tc.get("function", {}).get("name", "")
                arguments_str = tc.get("function", {}).get("arguments", "{}")
                tool_id = tc.get("id", "unknown")

                emit("tool_call", {"tool": tool_name, "arguments": arguments_str})

                result = await self._execute_tool(
                    user_id=user_id,
                    tool_name=tool_name,
                    arguments_str=arguments_str,
                    state=state,
                )

                emit("tool_result", {"tool": tool_name, "result": result})

                # 记录到 messages，让 LLM 看到结果
                # P1-2: 截断时追加标记，让 LLM 知道数据被截断
                raw_json = json.dumps(result, ensure_ascii=False)
                if len(raw_json) > 1500:
                    tool_content = raw_json[:1500] + f"...[truncated, original size: {len(raw_json)} bytes]"
                else:
                    tool_content = raw_json
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": tool_content,
                    }
                )

                # 提前终止：final_answer
                if tool_name == "final_answer":
                    final_message = result.get("message", "")
                    emit("final", {"message": final_message})
                    state.step = self.constraints.max_steps  # 强制退出循环
                    break

                # 提前终止：需要用户澄清
                if result.get("needs_clarification"):
                    final_message = result.get("question", "")
                    emit(
                        "clarify",
                        {
                            "question": final_message,
                            "options": result.get("options", []),
                        },
                    )
                    state.step = self.constraints.max_steps
                    break

        if state.step >= self.constraints.max_steps and not final_message:
            final_message = "操作步骤过多，已暂停。请告诉我更具体的需求，或从候选照片中选择。"
            emit("final", {"message": final_message})

        return state, events

    def _build_context(
        self, messages: list[dict], state: AgentState
    ) -> list[dict]:
        """在已有 messages 后追加当前状态摘要，让 LLM 做 informed 决策。

        每步循环移除上一步的上下文摘要（role=system, name=context），
        替换为最新 summary，避免消息列表无限累积。
        """
        remaining_steps = self.constraints.max_steps - state.step
        # P2-2: 步数接近上限时追加预警提示
        step_warning = ""
        if remaining_steps <= 2:
            step_warning = " ⚠ 剩余步数不足，请尽快给出最终答案。"

        summary = json.dumps(
            {
                "step": state.step,
                "max_steps": self.constraints.max_steps,
                "remaining_steps": remaining_steps,
                "search_attempts": state.search_attempts,
                "max_searches": self.constraints.max_searches,
                "rejected_photo_ids": list(state.rejected_photo_ids),
                "confirmed_photo_id": state.confirmed_photo_id,
                "fallback_level": state.fallback_level,
                "last_search_count": len(state.last_search_items),
                # P1-3: 预算信息让 LLM 感知剩余资源
                "total_tokens": state.total_tokens,
                "max_total_tokens": self.constraints.max_total_tokens,
                "total_cost": round(state.total_cost, 2),
                "max_cost_yuan": self.constraints.max_cost_yuan,
            },
            ensure_ascii=False,
        )
        # 移除上一步的上下文摘要，替换为最新 summary（避免累积）
        filtered = [
            m
            for m in messages
            if not (m.get("role") == "system" and m.get("name") == "context")
        ]
        filtered.append(
            {
                "role": "system",
                "name": "context",
                "content": f"当前运行状态：{summary}{step_warning}",
            }
        )
        return filtered

    async def _execute_tool(
        self,
        user_id: UUID,
        tool_name: str,
        arguments_str: str,
        state: AgentState,
    ) -> dict:
        """根据 tool 名称分发并执行，同时更新 State。"""
        # final_answer 是伪工具，直接返回
        if tool_name == "final_answer":
            try:
                args = json.loads(arguments_str)
                return {"ok": True, "message": args.get("message", "")}
            except json.JSONDecodeError:
                return {"ok": True, "message": arguments_str}

        spec = self.registry.get(tool_name)
        if spec is None:
            return {"ok": False, "error": f"未知工具：{tool_name}"}

        try:
            args = json.loads(arguments_str) if arguments_str else {}
        except json.JSONDecodeError:
            return {"ok": False, "error": f"参数不是合法 JSON：{arguments_str}"}

        # 注入公共参数
        args.setdefault("user_id", user_id)
        args.setdefault("db", self.db)

        # 类型转换：LLM 返回的 UUID 字段是字符串，转为 UUID 对象
        for uuid_field in ("photo_id", "skill_id"):
            val = args.get(uuid_field)
            if val is not None and not isinstance(val, UUID):
                try:
                    args[uuid_field] = UUID(str(val))
                except (ValueError, AttributeError):
                    return {"ok": False, "error": f"无效的 {uuid_field}: {val}"}

        # 特殊业务逻辑：search_photos 次数限制
        if tool_name == "search_photos":
            if state.search_attempts >= self.constraints.max_searches:
                return {
                    "ok": False,
                    "hint": f"已达到最大搜索次数（{self.constraints.max_searches}），"
                    "建议调用 fallback_search 兜底或向用户确认需求。",
                }
            state.search_attempts += 1

        # 特殊业务逻辑：ask_clarification 次数限制
        if tool_name == "ask_clarification":
            if state.clarification_attempts >= self.constraints.max_clarifications:
                return {
                    "ok": False,
                    "hint": f"已达到最大澄清次数（{self.constraints.max_clarifications}），"
                    "请直接根据现有信息给出最佳结果。",
                }
            state.clarification_attempts += 1

        # P1-1: 不可逆动作代码级确认 — apply_skill 必须有已选中的照片
        if tool_name == "apply_skill":
            if not state.confirmed_photo_id and not state.last_search_items:
                return {
                    "ok": False,
                    "error_type": "confirmation_required",
                    "hint": "请先搜索或浏览照片，选中一张后再进行 AI 改造。",
                }
            # P0-1: 会话级费用预算检查
            if state.total_cost >= self.constraints.max_cost_yuan:
                return {
                    "ok": False,
                    "error_type": "cost_budget_exceeded",
                    "hint": f"本会话生成费用已达上限（{self.constraints.max_cost_yuan}元），"
                    "请开启新会话。",
                }

        # P0-2: 工具执行超时保护
        tool_timeout = spec.timeout or self.constraints.tool_timeout
        try:
            result = await asyncio.wait_for(spec.fn(**args), timeout=tool_timeout)
        except TimeoutError:
            logger.warning("Tool execution timed out | tool=%s timeout=%ds", tool_name, tool_timeout)
            return {
                "ok": False,
                "error_type": "timeout",
                "error": f"工具 {tool_name} 执行超时（{tool_timeout}s）",
            }
        except Exception as exc:
            logger.exception("Tool execution failed | tool=%s", tool_name)
            return {"ok": False, "error_type": _classify_exception(exc), "error": str(exc)}

        # 主动澄清：普通搜索失败 2 次后自动生成引导选项
        if (
            tool_name == "search_photos"
            and result.get("ok")
            and not result.get("items")
            and state.search_attempts >= 2
            and state.clarification_attempts < self.constraints.max_clarifications
        ):
            state.clarification_attempts += 1
            return await _generate_clarification(state.original_query)

        # 自动兜底：普通搜索无结果且未走过兜底时，触发三级兜底
        # start_level=1 跳过 Level 0（刚搜过且无结果，避免重复搜索）
        if (
            tool_name == "search_photos"
            and result.get("ok")
            and not result.get("items")
            and self.constraints.enable_browse_fallback
            and state.fallback_level == 0
        ):
            fallback_result = await fallback_search(
                user_id=user_id,
                db=self.db,
                query=args.get("query", ""),
                from_date=args.get("from_date"),
                to_date=args.get("to_date"),
                limit=args.get("limit", 30),
                start_level=1,
            )
            state.fallback_level = fallback_result.get("fallback_level", 0)
            result = fallback_result

        # 更新 State
        if tool_name in ("search_photos", "fallback_search") and result.get("ok"):
            state.last_search_items = result.get("items", [])
            state.fallback_level = result.get("fallback_level", state.fallback_level)
        elif tool_name == "apply_skill" and result.get("ok"):
            state.confirmed_generation_id = result.get("generation_id")
            # P0-1/P1-3: 追踪会话级费用（wanx2.1-imageedit 约 0.14 元/次）
            state.total_cost += 0.14

        return result
