import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request

from app.main import app
from app.schemas.auth import WebRegisterRequest
from app.services import web_auth


def test_salted_password_roundtrip_and_unicode():
    password = "a long password 密码 🔒"
    first, second = web_auth.hash_password(password), web_auth.hash_password(password)
    assert first != second and password not in first
    assert web_auth.verify_password(password, first)
    assert not web_auth.verify_password(password + "x", first)
    assert not web_auth.verify_password(password, "invalid")
    assert not web_auth.verify_password(password, web_auth.DUMMY_HASH)


def test_normalized_username_and_masked_password_repr():
    value = WebRegisterRequest(username="  My_User  ", password="a long password!")
    assert value.username == "my_user"
    assert "a long password!" not in repr(value)


@pytest.mark.asyncio
async def test_invalid_password_never_in_response_or_validation_log(caplog):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for password in ("secret", "secret" * 30):
            response = await client.post(
                "/auth/register", json={"username": "valid_user", "password": password}
            )
            assert response.status_code == 422
            assert password not in response.text
            assert password not in caplog.text


@pytest.mark.asyncio
async def test_redis_failure_closes_auth_and_does_not_trust_forwarded_ip(monkeypatch):
    redis = AsyncMock()
    redis.eval.side_effect = ConnectionError("private backend connection")
    monkeypatch.setattr(web_auth, "get_redis", AsyncMock(return_value=redis))
    request = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 1),
            "headers": [(b"x-forwarded-for", b"spoofed")],
        }
    )
    with pytest.raises(HTTPException) as failure:
        await web_auth.throttle(request, "test_user")
    assert failure.value.status_code == 503
    assert "private" not in failure.value.detail
    import hashlib

    assert (
        "auth:login:" + hashlib.sha256(b"ip:127.0.0.1").hexdigest()
        in redis.eval.call_args.args
    )


@pytest.mark.asyncio
async def test_throttle_cancellation_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        web_auth, "get_redis", AsyncMock(side_effect=asyncio.CancelledError)
    )
    request = Request({"type": "http", "client": ("127.0.0.1", 1), "headers": []})
    with pytest.raises(asyncio.CancelledError):
        await web_auth.throttle(request, "test_user")
