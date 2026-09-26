"""认证相关 schema."""

from pydantic import BaseModel, Field, SecretStr, field_validator
import re


class WebLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        if not isinstance(value, str):
            raise ValueError("账号需要是文本")
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,32}", value):
            raise ValueError("账号需为3至32位英文字母、数字或下划线")
        return value


class WebRegisterRequest(WebLoginRequest):
    password: SecretStr = Field(min_length=12, max_length=128)


class AuthOptions(BaseModel):
    registration_enabled: bool
    development_login_enabled: bool


class LoginRequest(BaseModel):
    """小程序 wx.login() 拿到的临时 code."""

    code: str = Field(..., min_length=1, max_length=128, description="wx.login code")
    nickname: str | None = None
    avatar_url: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
