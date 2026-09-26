"""Real SQL/Redis and HTTP; never uses the running application's data."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.core.security import decode_token
from app.database import get_db
from app.main import app
from app.models.user import User
from app.models.web_credential import WebCredential
from app.services import web_auth

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]
PASSWORD = "test password for web!"


@pytest_asyncio.fixture
async def auth_env(monkeypatch):
    url = make_url(settings.database_url)
    assert (
        url.host in {"127.0.0.1", "localhost"}
        and url.database == "photo_agent_batch1_test"
    )
    redis_url = make_url(settings.redis_url)
    assert redis_url.host in {"127.0.0.1", "localhost"} and redis_url.database == "15"
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, hide_parameters=True
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    prefix = "auth_test_" + uuid4().hex[:10]

    async def db_session():
        async with factory() as db:
            yield db

    async def redis_client():
        return redis

    app.dependency_overrides[get_db] = db_session
    monkeypatch.setattr(web_auth, "get_redis", redis_client)
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "web_registration_enabled", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, factory, redis, prefix
    app.dependency_overrides.pop(get_db, None)
    async with factory() as db:
        await db.execute(delete(User).where(User.nickname.startswith(prefix)))
        await db.commit()
    keys = [key async for key in redis.scan_iter("auth:*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_production_registration_login_identity_and_wechat_isolation(auth_env):
    client, factory, _, prefix = auth_env
    # A matching nickname must not attach credentials to a WeChat identity.
    legacy_id = uuid4()
    async with factory() as db:
        db.add(User(id=legacy_id, wechat_openid="legacy-" + prefix, nickname=prefix))
        await db.commit()
    result = await client.post(
        "/auth/register", json={"username": prefix.upper(), "password": PASSWORD}
    )
    assert result.status_code == 201, result.text
    assert result.headers["cache-control"] == "no-store"
    token = result.json()["access_token"]
    user_id = decode_token(token)
    assert user_id != legacy_id
    async with factory() as db:
        credential = await db.get(WebCredential, user_id)
        assert credential.username == prefix and credential.password_hash != PASSWORD
        assert (await db.get(User, user_id)).wechat_openid is None
    me = await client.get("/auth/me", headers={"Authorization": "Bearer " + token})
    assert me.status_code == 200 and me.json()["id"] == str(user_id)
    assert "password" not in me.text and "username" not in me.text
    login = await client.post(
        "/auth/login", json={"username": prefix, "password": PASSWORD}
    )
    assert (
        login.status_code == 200
        and decode_token(login.json()["access_token"]) == user_id
    )
    assert (await client.get("/auth/me")).status_code == 401
    from jose import jwt

    expired = jwt.encode(
        {"sub": str(user_id), "exp": 1},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    response = await client.get(
        "/auth/me", headers={"Authorization": "Bearer " + expired}
    )
    assert response.status_code == 401 and response.json()["errNo"] == 10001
    assert (
        await client.get("/auth/me", headers={"Authorization": "Bearer invalid"})
    ).status_code == 401
    assert not (await client.get("/auth/options")).json()["development_login_enabled"]


@pytest.mark.asyncio
async def test_registration_race_rolls_back_orphan_users(auth_env):
    client, factory, _, prefix = auth_env
    results = await asyncio.gather(
        *[
            client.post(
                "/auth/register", json={"username": prefix, "password": PASSWORD}
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(User).where(User.nickname == prefix)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_account_are_indistinguishable(auth_env):
    client, _, _, prefix = auth_env
    await client.post("/auth/register", json={"username": prefix, "password": PASSWORD})
    wrong = await client.post(
        "/auth/login", json={"username": prefix, "password": "wrong"}
    )
    missing = await client.post(
        "/auth/login", json={"username": prefix + "x", "password": "wrong"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()


@pytest.mark.asyncio
async def test_closed_registration_still_allows_login(auth_env, monkeypatch):
    client, _, _, prefix = auth_env
    await client.post("/auth/register", json={"username": prefix, "password": PASSWORD})
    monkeypatch.setattr(settings, "web_registration_enabled", False)
    assert (
        await client.post(
            "/auth/register", json={"username": prefix + "x", "password": PASSWORD}
        )
    ).status_code == 403
    assert not (await client.get("/auth/options")).json()["registration_enabled"]
    assert (
        await client.post(
            "/auth/login", json={"username": prefix, "password": PASSWORD}
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_distributed_limit_and_retry_after(auth_env):
    client, _, redis, prefix = auth_env
    responses = await asyncio.gather(
        *[
            client.post(
                "/auth/login", json={"username": prefix, "password": "incorrect"}
            )
            for _ in range(12)
        ]
    )
    assert sorted(r.status_code for r in responses) == [401] * 10 + [429] * 2
    assert all(
        int(r.headers["retry-after"]) > 0 for r in responses if r.status_code == 429
    )
    keys = [key async for key in redis.scan_iter("auth:login:*")]
    assert all([prefix not in key and 0 < await redis.ttl(key) <= 900 for key in keys])
