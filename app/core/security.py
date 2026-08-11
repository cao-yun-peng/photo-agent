"""JWT 签发与校验、当前用户依赖."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import (
    ApiError,
    AUTH_JWT_EXPIRED,
    AUTH_JWT_INVALID,
    AUTH_PERMISSION_DENIED,
    AUTH_USER_NOT_FOUND,
)
from app.core.logger import get_logger, set_logging_context
from app.database import get_db
from app.models.user import User

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(user_id: UUID) -> str:
    """签发 JWT。sub 存 user_id。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> UUID:
    """解析 JWT，返回 user_id。失败抛 ApiError。"""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        sub = payload.get("sub")
        if not sub:
            raise ValueError("missing sub")
        return UUID(sub)
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(AUTH_JWT_EXPIRED) from exc
    except (JWTError, ValueError) as exc:
        raise ApiError(AUTH_JWT_INVALID, message=f"Invalid token: {exc}") from exc


async def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI Depends：从 Authorization 头解析并加载 User。

    认证成功后自动将userId注入日志上下文，实现全链路追踪。
    """
    if credentials is None:
        raise ApiError(AUTH_JWT_INVALID, message="Missing Authorization header")

    user_id = decode_token(credentials.credentials)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise ApiError(AUTH_USER_NOT_FOUND)

    # 认证成功：将用户ID注入日志上下文
    set_logging_context(user_id=str(user.id))
    request.state.user_id = str(user.id)

    return user


async def get_current_user_optional(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """可选认证：不强制要求登录，未登录时返回None。"""
    if credentials is None:
        return None

    try:
        user_id = decode_token(credentials.credentials)
    except ApiError:
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is not None:
        set_logging_context(user_id=str(user.id))
        request.state.user_id = str(user.id)

    return user
