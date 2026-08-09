"""Pydantic 请求/响应体."""
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserOut
from app.schemas.photo import (
    UploadUrlRequest,
    UploadUrlResponse,
    PhotoCreate,
    PhotoOut,
    PhotoListItem,
    SearchQuery,
    SearchResult,
)
from app.schemas.skill import (
    SkillCreate,
    SkillUpdate,
    SkillOut,
    GenerateRequest,
    GenerationOut,
    QuotaInfo,
)

__all__ = [
    "LoginRequest",
    "TokenResponse",
    "UserOut",
    "UploadUrlRequest",
    "UploadUrlResponse",
    "PhotoCreate",
    "PhotoOut",
    "PhotoListItem",
    "SearchQuery",
    "SearchResult",
    "SkillCreate",
    "SkillUpdate",
    "SkillOut",
    "GenerateRequest",
    "GenerationOut",
    "QuotaInfo",
]
