"""图像生成供应商契约的回归测试。"""
from __future__ import annotations

import base64
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from app.services import image_gen
from app.workers.gen_tasks import _build_gen_key


class _FakeImageClient:
    def __init__(self, *, fail_url: str | None = None, **_: Any) -> None:
        self.fail_url = fail_url
        self.post_call: dict[str, Any] | None = None

    async def __aenter__(self) -> _FakeImageClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str) -> httpx.Response:
        status = 403 if url == self.fail_url else 200
        content_type = "image/png" if "ref" in url else "image/jpeg"
        return httpx.Response(
            status,
            content=b"input-image",
            headers={"content-type": content_type},
            request=httpx.Request("GET", url),
        )

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.post_call = {"url": url, **kwargs}
        encoded = base64.b64encode(b"generated-png").decode("ascii")
        return httpx.Response(
            200,
            json={"data": [{"b64_json": encoded}]},
            request=httpx.Request("POST", url),
        )


@pytest.mark.asyncio
async def test_gpt_image_2_uses_multi_image_contract_and_decodes_result() -> None:
    client = _FakeImageClient()
    with (
        patch.object(image_gen.settings, "openai_api_key", "sk-test"),
        patch.object(image_gen.httpx, "AsyncClient", return_value=client),
    ):
        result = await image_gen._generate_gpt_image(
            "https://example.com/source.jpg",
            "转换成插画",
            ["https://example.com/ref.png"],
        )

    assert result.model == "gpt-image-2"
    assert result.image_bytes == b"generated-png"
    assert result.image_url is None
    assert result.content_type == "image/png"
    assert client.post_call is not None
    assert client.post_call["data"]["model"] == "gpt-image-2"
    assert [part[0] for part in client.post_call["files"]] == ["image[]", "image[]"]
    assert client.post_call["files"][1][1][2] == "image/png"


@pytest.mark.asyncio
async def test_gpt_image_2_rejects_failed_input_download() -> None:
    source_url = "https://example.com/source.jpg"
    client = _FakeImageClient(fail_url=source_url)
    with (
        patch.object(image_gen.settings, "openai_api_key", "sk-test"),
        patch.object(image_gen.httpx, "AsyncClient", return_value=client),
        pytest.raises(image_gen.GenerationError, match="download input image HTTP 403"),
    ):
        await image_gen._generate_gpt_image(source_url, "转换成插画", [])


def test_generated_oss_key_matches_content_type() -> None:
    user_id = uuid4()
    assert _build_gen_key(user_id, "image/png").endswith(".png")
    assert _build_gen_key(user_id, "image/webp").endswith(".webp")
    assert _build_gen_key(user_id, "image/jpeg").endswith(".jpg")
