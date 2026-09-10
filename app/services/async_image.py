"""TiMi async transport. Submit once; each poll only reads an existing task."""

import base64
import io
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from PIL import Image

from app.config import settings
from app.models.provider_operation import ProviderCall
from app.services.generation_accounting import safe_usage
from app.services.image_gen import GenResult, GenerationError
from app.services.provider_contract import image_contract, SIZES
from app.services.provider_ledger import recorded_post

TIMEOUT = httpx.Timeout(60, connect=10)
MAX_BYTES = 18 * 1024 * 1024


class AsyncImageFailed(GenerationError):
    """Provider explicitly declared terminal failure; do not resubmit."""

    def __init__(self, code="provider_failed"):
        self.code = code
        super().__init__("Async image provider reported failure")


def task_endpoint(task):
    base = settings.openai_base_url.rstrip("/")
    image_contract(base, "timicc_async")
    if task.get("base_url") != base or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", task.get("id", "")
    ):
        raise GenerationError("Async task endpoint mismatch")
    return base + "/images/tasks/" + task["id"]


async def submit(prompt, images, size):
    contract = image_contract(settings.openai_base_url, "timicc_async")
    if size not in SIZES or not 1 <= len(images) <= 5 or len(prompt) > 32000:
        raise GenerationError("Unsupported async image inputs")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await recorded_post(
            client,
            contract["endpoint"],
            model="gpt-image-2",
            headers={"Authorization": "Bearer " + settings.openai_api_key},
            data={"model": "gpt-image-2", "prompt": prompt, "size": size, "n": 1},
            files=[
                ("image[]", (f"input-{i}.png", content, "image/png"))
                for i, content in enumerate(images)
            ],
        )
    if response.status_code != 202:
        raise GenerationError(f"Async image submit HTTP {response.status_code}")
    body = response.json()
    task = {
        "id": body.get("task_id") or body.get("id"),
        "base_url": settings.openai_base_url.rstrip("/"),
        "call_id": response.extensions.get("provider_call_id"),
        "deadline": time.time() + 45 * 60,
    }
    task_endpoint(task)
    return task


async def poll(task, factory, user_id, generation_id):
    """One bounded GET and optional download; None means keep waiting."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            task_endpoint(task),
            headers={"Authorization": "Bearer " + settings.openai_api_key},
        )
        response.raise_for_status()
        body = response.json()
        status = body.get("status")
        if status in {"pending", "queued", "processing"}:
            return None
        if status not in {"completed", "failed"}:
            raise GenerationError("Unknown async image task status")
        usage = safe_usage((body.get("result") or {}).get("usage"))
        provider_error = (body.get("error") or {}).get("type") or (
            body.get("error") or {}
        ).get("code")
        error_code = (
            "provider_rate_limited"
            if provider_error in {"rate_limit_error", "rate_limit_exceeded"}
            else "provider_failed"
        )
        if task.get("call_id"):
            async with factory() as db:
                row = await db.get(ProviderCall, UUID(task["call_id"]))
                if (
                    row is None
                    or row.user_id != user_id
                    or row.generation_id != generation_id
                ):
                    raise GenerationError("Async provider ledger ownership mismatch")
                row.status = "received" if status == "completed" else "provider_failed"
                row.details = {
                    **row.details,
                    "async_status": status,
                    "usage": usage,
                    "estimated_yuan": row.details["price"]["unit_yuan"]
                    if status == "completed"
                    else None,
                }
                if status == "failed":
                    row.details["failure"] = {"code": error_code}
                await db.commit()
        if status == "failed":
            raise AsyncImageFailed(error_code)
        encoded = (((body.get("result") or {}).get("data")) or [{}])[0].get("b64_json")
        if encoded:
            if not isinstance(encoded, str) or len(encoded) > 24 * 1024 * 1024:
                raise GenerationError("Async result exceeds image size limit")
            content = base64.b64decode(encoded, validate=True)
        else:
            url = body.get("image_url") or ""
            parsed = urlsplit(url)
            # Verified provider serves signed results on its own origin. Never forward credentials.
            if (
                parsed.scheme != "https"
                or parsed.hostname != "timicc.com"
                or parsed.port not in {None, 443}
                or parsed.username
                or parsed.password
            ):
                raise GenerationError("Unsupported async result origin")
            chunks, length = [], 0
            async with client.stream("GET", url) as download:
                download.raise_for_status()
                async for chunk in download.aiter_bytes():
                    length += len(chunk)
                    if length > MAX_BYTES:
                        raise GenerationError("Async result exceeds image size limit")
                    chunks.append(chunk)
            content = b"".join(chunks)
    with Image.open(io.BytesIO(content)) as image:
        if image.format != "PNG" or image.width * image.height > 20_000_000:
            raise GenerationError("Unsupported async result image")
        image.verify()
    return GenResult(
        model="gpt-image-2",
        image_bytes=content,
        content_type="image/png",
        cost_yuan=settings.package_generation_estimated_cost_yuan,
        usage=usage,
    )
