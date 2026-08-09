"""认证相关 schema."""
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """小程序 wx.login() 拿到的临时 code."""

    code: str = Field(..., min_length=1, max_length=128, description="wx.login code")
    nickname: str | None = None
    avatar_url: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
