"""PhotoAgent constraints, runtime state, and JSON serialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.config import settings
from app.services.rollout import agent_variant_for_user


@dataclass
class AgentConstraints:
    """Agent 运行约束。"""

    max_steps: int = 8
    max_searches: int = 2
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
    workflow_state: str = "idle"
    agent_variant: str = "control"

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

    # 面向模型的短期记忆。recent_messages 只保存用户/助手自然语言，
    # 不混入 reasoning；active_search 由服务端维护，是续搜时的可信工作状态。
    recent_messages: list[dict] = field(default_factory=list)
    conversation_summary: str = ""
    active_intent: str | None = None
    active_search: dict = field(default_factory=dict)
    pending_clarification: dict | None = None
    followup_type: str | None = None

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
            "workflow_state": self.workflow_state,
            "agent_variant": self.agent_variant,
            "step": self.step,
            "search_attempts": self.search_attempts,
            "clarification_attempts": self.clarification_attempts,
            "rejected_photo_ids": list(self.rejected_photo_ids),
            "confirmed_photo_id": self.confirmed_photo_id,
            "confirmed_generation_id": self.confirmed_generation_id,
            "last_search_items": self.last_search_items,
            "recent_messages": self.recent_messages,
            "conversation_summary": self.conversation_summary,
            "active_intent": self.active_intent,
            "active_search": self.active_search,
            "pending_clarification": self.pending_clarification,
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
            workflow_state=data.get("workflow_state", "idle"),
            agent_variant=data.get(
                "agent_variant", agent_variant_for_user(data.get("user_id", ""))
            ),
            step=data.get("step", 0),
            search_attempts=data.get("search_attempts", 0),
            clarification_attempts=data.get("clarification_attempts", 0),
            rejected_photo_ids=set(data.get("rejected_photo_ids", [])),
            confirmed_photo_id=data.get("confirmed_photo_id"),
            confirmed_generation_id=data.get("confirmed_generation_id"),
            last_search_items=data.get("last_search_items", []),
            recent_messages=data.get("recent_messages", []),
            conversation_summary=data.get("conversation_summary", ""),
            active_intent=data.get("active_intent"),
            active_search=data.get("active_search", {}),
            pending_clarification=data.get("pending_clarification"),
            fallback_level=data.get("fallback_level", 0),
            total_tokens=data.get("total_tokens", 0),
            total_cost=data.get("total_cost", 0.0),
            history=data.get("history", []),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
