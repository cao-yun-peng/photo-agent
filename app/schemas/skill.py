"""Skill / Generation 相关 schema."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = None
    prompt_template: str = Field(..., min_length=1, max_length=2000)
    reference_keys: list[str] = Field(default_factory=list, max_length=8)
    cover_key: str | None = None
    model: str = Field(
        default="wanx2.1-imageedit", pattern="^(wanx2\\.1-imageedit|gpt-image-2)$"
    )
    function: str = Field(
        default="description_edit",
        pattern="^(description_edit|stylization_all|stylization_local)$",
    )
    strength: float = Field(default=0.7, ge=0.0, le=1.0)
    is_public: bool = False


class SkillUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    prompt_template: str | None = None
    reference_keys: list[str] | None = None
    cover_key: str | None = None
    model: str | None = None
    function: str | None = Field(
        default=None, pattern="^(description_edit|stylization_all|stylization_local)$"
    )
    strength: float | None = Field(default=None, ge=0.0, le=1.0)
    is_public: bool | None = None


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID | None
    name: str
    description: str | None
    prompt_template: str
    reference_keys: list[str]
    cover_url: str | None = None  # 服务器动态签的公网 URL
    cover_key: str | None = None
    model: str
    function: str
    strength: float
    is_public: bool
    is_official: bool
    use_count: int
    created_at: datetime


class GenerateRequest(BaseModel):
    skill_id: UUID | None = None  # 不给就是"纯自由生成"
    extra_prompt: str | None = Field(default=None, max_length=500)
    model: str | None = Field(
        default=None, pattern="^(wanx2\\.1-imageedit|gpt-image-2)$"
    )
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class GenerationConfirmRequest(BaseModel):
    confirmation_token: UUID


class GenerationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_photo_id: UUID | None
    skill_id: UUID | None
    extra_prompt: str | None
    result_oss_key: str | None
    result_url: str | None = None  # 动态签
    status: str
    error_message: str | None
    model: str
    cost_yuan: Decimal
    estimated_cost_yuan: Decimal = Decimal("0")
    confirmation_token: UUID | None = None
    confirmation_expires_at: datetime | None = None
    enqueue_status: str = "not_queued"
    attempt_count: int = 0
    created_at: datetime


class QuotaInfo(BaseModel):
    used: int
    quota: int
    remaining: int
