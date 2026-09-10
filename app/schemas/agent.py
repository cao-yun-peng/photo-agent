"""Agent 对话相关 schema."""

from __future__ import annotations

from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentUIAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["reject_photo", "undo_feedback", "continue_search"]
    photo_id: UUID | None = None
    batch_id: str | None = Field(default=None, pattern="^[a-f0-9]{32}$")
    undo_id: str | None = Field(default=None, pattern="^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def validate_target(self):
        if self.action == "reject_photo":
            if not self.photo_id or not self.batch_id or self.undo_id:
                raise ValueError("reject_photo requires photo_id and batch_id only")
        elif self.action == "undo_feedback":
            if not self.undo_id or self.photo_id or self.batch_id:
                raise ValueError("undo_feedback requires undo_id only")
        elif self.photo_id or self.batch_id or self.undo_id:
            raise ValueError("continue_search has no target parameters")
        return self


class AgentRunRequest(BaseModel):
    """运行 Agent 的请求。"""

    ui_action: AgentUIAction | None = None
    query: str = Field(..., min_length=1, max_length=500)
    session_id: UUID | None = Field(default=None, description="续接已有会话 ID")
    feedback_batch_id: str | None = Field(
        default=None,
        pattern="^[a-f0-9]{32}$",
        description="用户反馈所针对的结果批次；旧客户端可省略",
    )
    selected_photo_id: UUID | None = Field(
        default=None,
        description="用户在当前候选列表中明确点击选择的照片 ID",
    )


class AgentRunResponse(BaseModel):
    """Agent 运行结果。"""

    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    events: list[dict]
    state: dict
    status: str
