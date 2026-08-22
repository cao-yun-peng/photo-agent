"""PhotoAgent 编排与兼容入口。

负责会话状态、确定性快路径、LLM 决策、工具调度与事件输出。
具体业务工具位于 ``agent_tools``，工作流状态转换位于 ``agent_workflow``。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.registry import prompt_registry
from app.core.telemetry import traced_async
from app.services.agent_tools import (
    _classify_exception,
    apply_skill,
    browse_candidates,
    fallback_search,
    get_photo_detail,
    recommend_skills_for_agent,
    search_photos,
)
from app.services.search_candidate_pool import (
    candidate_pool_key,
    candidate_pool_size,
    get_candidate_trace_context,
    get_prefetch_status,
    pop_verified_candidate,
    set_prefetch_status,
    wait_for_verified_candidate,
)
from app.services.search_index import enqueue_index_repairs

from app.services.agent_execution import (
    AgentExecutionDependencies,
    execute_tool,
)
from app.services.agent_runtime import (
    AgentRuntimeDependencies,
    build_context,
    run_agent,
)

from app.services.agent_intent import (
    _detect_followup_type,
    _requested_user_selection_limit,
    _requests_complete_result_set,
)
from app.services.agent_llm import _is_mock_llm, _llm_decide
from app.services.agent_messages import (
    _fast_search_message,
    _model_tool_content,
    _remember_message,
    _search_coverage_complete,
    _search_result_fallback_message,
)
from app.services.agent_registry import (
    DEFAULT_REGISTRY,
    ToolRegistry,
    ToolSpec,
    _build_registry,
    ask_clarification,
)
from app.services.agent_state import AgentConstraints, AgentState

__all__ = [
    "AgentConstraints",
    "AgentState",
    "PhotoAgent",
    "ToolRegistry",
    "ToolSpec",
    "DEFAULT_REGISTRY",
    "ask_clarification",
    "_build_registry",
    "_is_mock_llm",
    "_llm_decide",
    "_detect_followup_type",
    "_requested_user_selection_limit",
    "_requests_complete_result_set",
    "_fast_search_message",
    "_model_tool_content",
    "_remember_message",
    "_search_coverage_complete",
    "_search_result_fallback_message",
    "_classify_exception",
    "apply_skill",
    "browse_candidates",
    "fallback_search",
    "get_photo_detail",
    "recommend_skills_for_agent",
    "search_photos",
    "candidate_pool_key",
    "candidate_pool_size",
    "get_candidate_trace_context",
    "get_prefetch_status",
    "pop_verified_candidate",
    "set_prefetch_status",
    "wait_for_verified_candidate",
    "enqueue_index_repairs",
]


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
        # 优先使用传入的prompt，否则从热更新注册表获取
        self.system_prompt = system_prompt or prompt_registry.get_agent_system_prompt()

    def _tool_schemas_for_state(self, state: AgentState) -> list[dict]:
        if state.agent_variant != "v2":
            return self.registry.schemas()
        # v2 只向模型暴露业务级动作；浏览、兜底和详情查询仍由代码内部调用。
        return self.registry.schemas(
            {
                "search_photos",
                "ask_clarification",
                "apply_skill",
                "recommend_skills",
            }
        )

    @traced_async(
        "invoke_agent photo-search",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "photo-search",
        },
    )
    async def run(
        self,
        user_id: UUID,
        query: str,
        session_id: UUID | None = None,
        initial_state: AgentState | None = None,
        event_queue: asyncio.Queue | None = None,
    ) -> tuple[AgentState, list[dict]]:
        dependencies = AgentRuntimeDependencies(
            llm_decide=_llm_decide,
            browse_candidates=browse_candidates,
            get_prefetch_status=get_prefetch_status,
            wait_for_verified_candidate=wait_for_verified_candidate,
            pop_verified_candidate=pop_verified_candidate,
            get_candidate_trace_context=get_candidate_trace_context,
            candidate_pool_size=candidate_pool_size,
            set_prefetch_status=set_prefetch_status,
        )
        return await run_agent(
            self,
            dependencies,
            user_id,
            query,
            session_id=session_id,
            initial_state=initial_state,
            event_queue=event_queue,
        )

    def _build_context(self, messages: list[dict], state: AgentState) -> list[dict]:
        return build_context(self, messages, state)

    async def _execute_tool(
        self,
        user_id: UUID,
        tool_name: str,
        arguments_str: str,
        state: AgentState,
    ) -> dict:
        dependencies = AgentExecutionDependencies(
            candidate_pool_key=candidate_pool_key,
            enqueue_index_repairs=enqueue_index_repairs,
        )
        return await execute_tool(
            self, dependencies, user_id, tool_name, arguments_str, state
        )
