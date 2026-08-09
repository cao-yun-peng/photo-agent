"""Auth 路由：/auth/wechat 换 token、/me 拿当前用户."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import create_access_token, get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserOut
from app.services.wechat import WeChatError, code2session

router = APIRouter()


@router.post(
    "/wechat",
    response_model=TokenResponse,
    summary="小程序 code 换 JWT",
)
async def wechat_login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    try:
        session = await code2session(payload.code)
    except WeChatError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    openid = session["openid"]

    # 查找或创建用户
    result = await db.execute(select(User).where(User.wechat_openid == openid))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            wechat_openid=openid,
            nickname=payload.nickname,
            avatar_url=payload.avatar_url,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif payload.nickname or payload.avatar_url:
        # 首次授权后可能带来了昵称头像，顺便更新
        if payload.nickname:
            user.nickname = payload.nickname
        if payload.avatar_url:
            user.avatar_url = payload.avatar_url
        await db.commit()

    token = create_access_token(user.id)
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.get(
    "/me",
    response_model=UserOut,
    summary="当前用户信息（用于 JWT 联调）",
)
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    return current_user
