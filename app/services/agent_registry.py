"""Tool schemas and registry construction for PhotoAgent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.services.agent_tools import (
    apply_skill,
    browse_candidates,
    fallback_search,
    get_photo_detail,
    recommend_skills_for_agent,
    search_photos,
)


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

    def schemas(self, names: set[str] | None = None) -> list[dict]:
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
            if names is None or spec.name in names
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
                    "result_mode": {
                        "type": "string",
                        "enum": ["browse", "best", "select"],
                        "description": "browse=返回最多5张；best=系统比较Top-5后返回最佳1张；select=返回用户要求的数量或完整结果集，由用户本人选择",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "期望返回数量；select 模式必须原样尊重用户要求，不得擅自改成5或30",
                    },
                    "complete_result_set": {
                        "type": "boolean",
                        "description": "用户要求全部/所有匹配照片时必须为 true；此时 limit 不构成截断",
                    },
                    "photo_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "selfie",
                                "screenshot",
                                "group_photo",
                                "portrait",
                                "document",
                                "food",
                                "scenery",
                                "other",
                            ],
                        },
                        "description": "可选的结构化照片类型过滤；通常服务端会从中文查询自动推导",
                    },
                    "is_selfie": {
                        "type": "boolean",
                        "description": "是否只搜索自拍",
                    },
                    "people_count_min": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "最少人物数量",
                    },
                    "people_count_max": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "最多人物数量",
                    },
                    "min_semantic_score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "可选相似度阈值；精确集合搜索会自动绕过以防漏图",
                    },
                    "exclude_photo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "续搜时排除已经展示或被用户拒绝的照片 ID",
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
                    "exclude_photo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "排除已经展示或被用户拒绝的照片 ID",
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
            description="准备对指定照片应用 AI 改造；灰度版会返回费用摘要并等待用户再次确认后才入队。",
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
