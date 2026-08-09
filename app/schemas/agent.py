"""Agent 对话相关 schema."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentRunRequest(BaseModel):
    """运行 Agent 的请求。"""

    query: str = Field(..., min_length=1, max_length=500)
    session_id: UUID | None = Field(default=None, description="续接已有会话 ID")


class AgentRunResponse(BaseModel):
    """Agent 运行结果。"""

    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    events: list[dict]
    state: dict
    status: str
