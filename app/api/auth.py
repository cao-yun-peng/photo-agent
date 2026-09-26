"""Auth 路由：/auth/wechat 换 token、/me 拿当前用户."""

from typing import Annotated
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import create_access_token, get_current_user
from app.database import get_db
from app.models.user import User
from app.models.web_credential import WebCredential
from app.schemas.auth import (
    AuthOptions,
    LoginRequest,
    TokenResponse,
    WebLoginRequest,
    WebRegisterRequest,
)
from app.schemas.user import UserOut
from app.services.wechat import WeChatError, code2session
from app.services import web_auth

router = APIRouter()


@router.get("/options", response_model=AuthOptions)
async def auth_options() -> AuthOptions:
    return AuthOptions(
        registration_enabled=settings.web_registration_enabled,
        development_login_enabled=(
            settings.app_env == "dev"
            and not (
                settings.wechat_appid
                and settings.wechat_appid != "wx_your_appid"
                and settings.wechat_secret
                and settings.wechat_secret != "your_secret"
            )
        ),
    )


def _web_token(user_id, response: Response) -> TokenResponse:
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(
        access_token=create_access_token(user_id),
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
async def web_register(
    payload: WebRegisterRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    if not settings.web_registration_enabled:
        raise HTTPException(403, "当前暂未开放注册")
    await web_auth.throttle(request, payload.username, registration=True)
    password_hash = await asyncio.to_thread(
        web_auth.hash_password, payload.password.get_secret_value()
    )
    # One transaction: a raced duplicate account must not leave an orphan user.
    user = User(wechat_openid=None, nickname=payload.username)
    try:
        db.add(user)
        await db.flush()
        db.add(
            WebCredential(
                user_id=user.id, username=payload.username, password_hash=password_hash
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "该账号无法注册，请更换账号或直接登录") from None
    return _web_token(user.id, response)


@router.post("/login", response_model=TokenResponse)
async def web_login(
    payload: WebLoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    await web_auth.throttle(request, payload.username)
    credential = (
        await db.execute(
            select(WebCredential).where(WebCredential.username == payload.username)
        )
    ).scalar_one_or_none()
    valid = await asyncio.to_thread(
        web_auth.verify_password,
        payload.password.get_secret_value(),
        credential.password_hash if credential else web_auth.DUMMY_HASH,
    )
    if credential is None or not valid:
        raise HTTPException(401, "账号或密码不正确")
    return _web_token(credential.user_id, response)


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
