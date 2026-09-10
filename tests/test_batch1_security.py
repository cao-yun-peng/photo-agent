import io
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import _oss_mock, admin
from app.config import Settings, settings
from app.core.security import get_current_user
from app.services import oss


def runtime_settings(**kwargs):
    return Settings(
        _env_file=None,
        **{
            "database_url": "postgresql+asyncpg://test:test@localhost/test",
            "redis_url": "redis://localhost",
            "jwt_secret": "x" * 40,
            "app_env": "prod",
            "mock_oss_enabled": False,
            "oss_backend": "oss",
            "oss_endpoint": "example.com",
            "oss_bucket": "real-bucket",
            "oss_key_id": "real-id",
            "oss_key_secret": "secret",
            "dashscope_api_key": "test-only-dashscope",
            "openai_api_key": "test-only-openai",
            **kwargs,
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"dashscope_api_key": ""},
        {"dashscope_api_key": " sk-xxx "},
        {"openai_api_key": ""},
        {"openai_api_key": "sk-openai-xxx"},
        {"oss_backend": "mock"},
        {"mock_oss_enabled": True},
        {"oss_key_secret": ""},
        {"oss_key_id": "LTAI_xxx"},
        {"jwt_secret": "please_change_me_to_a_32_byte_random_string"},
        {"admin_enabled": True, "admin_user_ids": []},
    ],
)
def test_production_rejects_unsafe_startup(changes):
    with pytest.raises(ValueError):
        runtime_settings(**changes).validate_runtime()


def test_explicit_production_and_development_configuration():
    runtime_settings().validate_runtime()
    runtime_settings(
        app_env="dev", oss_backend="mock", mock_oss_enabled=True
    ).validate_runtime()
    with pytest.raises(ValueError):
        runtime_settings(
            app_env="dev", oss_backend="mock", mock_oss_enabled=False
        ).validate_runtime()


@pytest.mark.asyncio
async def test_admin_router_denies_dev_query_and_requires_allowlist(monkeypatch):
    app = FastAPI()
    app.include_router(admin.router)
    user = SimpleNamespace(id=uuid4())

    async def ordinary():
        return user

    app.dependency_overrides[get_current_user] = ordinary
    monkeypatch.setattr(settings, "admin_enabled", True)
    monkeypatch.setattr(settings, "admin_user_ids", [])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/admin/stats", "/admin/stats?dev_mode=true"):
            assert (await client.get(path)).status_code == 403
        monkeypatch.setattr(settings, "admin_user_ids", [user.id])
        assert (await client.get("/admin/stats")).status_code == 200
        monkeypatch.setattr(settings, "admin_enabled", False)
        assert (await client.get("/admin/stats")).status_code == 404

        async def anonymous():
            raise HTTPException(401)

        app.dependency_overrides[get_current_user] = anonymous
        assert (await client.post("/admin/refresh?dev_mode=true")).status_code == 401


@pytest.mark.parametrize(
    "key",
    [
        "../secret",
        "/secret",
        "a/../../secret",
        "C:/secret",
        "a\\secret",
        "a//b",
        "a/./b",
    ],
)
def test_all_mock_file_operations_reject_escape(tmp_path, monkeypatch, key):
    monkeypatch.setattr(oss, "_MOCK_ROOT", tmp_path / "root")
    for call in (
        lambda: oss.mock_read_object(key),
        lambda: oss.mock_write_object(key, io.BytesIO(b"x")),
        lambda: oss._head_object_sync(key),
        lambda: oss._delete_object_sync(key),
    ):
        with pytest.raises(ValueError):
            call()


def test_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS requires symlink privilege")
    monkeypatch.setattr(oss, "_MOCK_ROOT", root)
    with pytest.raises(ValueError):
        oss.mock_path("link/secret")


@pytest.mark.asyncio
async def test_mock_signed_streaming_limit_and_head(tmp_path, monkeypatch):
    monkeypatch.setattr(oss, "_MOCK_ROOT", tmp_path)
    monkeypatch.setattr(settings, "mock_oss_max_bytes", 5)
    app = FastAPI()
    app.include_router(_oss_mock.router)
    signed = oss.sign_put_url("photos/user/image.jpg", "image/jpeg")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.put("/_mock/oss/photos/user/image.jpg", content=b"x")
        ).status_code == 403
        assert (
            await client.put(signed.url, content=b"abc", headers=signed.headers)
        ).status_code == 200
        url = oss.sign_get_url("photos/user/image.jpg")
        assert (await client.get(url)).content == b"abc"
        assert (await client.head(url)).headers["content-length"] == "3"

        async def oversized():
            yield b"123"
            yield b"456"

        assert (
            await client.put(signed.url, content=oversized(), headers=signed.headers)
        ).status_code == 413
        assert (await client.get(url)).content == b"abc"
        assert len(list((tmp_path / "photos/user").iterdir())) == 1
        assert (
            await client.get(oss.mock_signed_url("GET", "photos/user/image.jpg", -5))
        ).status_code == 403
        assert (
            await client.put(
                signed.url.replace("image.jpg", "other.jpg"),
                content=b"x",
                headers=signed.headers,
            )
        ).status_code == 403
        monkeypatch.setattr(settings, "app_env", "prod")
        assert (await client.get(url)).status_code == 404
