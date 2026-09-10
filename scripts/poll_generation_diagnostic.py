"""Resume GET-only polling for an already-paid diagnostic task; never submits."""

import asyncio
import base64
import io
import json
import logging
from pathlib import Path
from time import perf_counter
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from PIL import Image

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models.provider_operation import ProviderCall
from app.services.generation_accounting import safe_usage


async def main(directory):
    engine.echo = False
    logging.disable(logging.CRITICAL)
    output = Path(directory)
    report = json.loads((output / "report.json").read_text())
    task_id = report["async_task_id"]
    call_id = UUID(report["provider_call_id"])
    endpoint = (
        settings.openai_base_url.rstrip("/")
        + "/images/tasks/"
        + quote(task_id, safe="")
    )
    started = perf_counter()
    previous = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
        while perf_counter() - started < 600:
            try:
                response = await client.get(
                    endpoint,
                    headers={"Authorization": "Bearer " + settings.openai_api_key},
                )
                response.raise_for_status()
                task = response.json()
                status = task.get("status")
                report["async_status"] = status
                if status != previous:
                    print(
                        json.dumps(
                            {
                                "task_id": task_id,
                                "status": status,
                                "resume_elapsed_s": round(perf_counter() - started),
                            }
                        ),
                        flush=True,
                    )
                    previous = status
                if status == "failed":
                    report["async_error"] = task.get("error")
                    report["outcome"] = "provider_failed"
                elif status == "completed":
                    result = task.get("result") or {}
                    encoded = (result.get("data") or [{}])[0].get("b64_json")
                    if encoded:
                        content = base64.b64decode(encoded, validate=True)
                    else:
                        url = task.get("image_url") or ""
                        if urlsplit(url).scheme != "https":
                            raise ValueError("Invalid image URL")
                        download = await client.get(url)
                        download.raise_for_status()
                        content = download.content
                    image = Image.open(io.BytesIO(content))
                    dimensions = list(image.size)
                    image.verify()
                    (output / "result.png").write_bytes(content)
                    report.update(
                        {
                            "outcome": "image_received",
                            "output_dimensions": dimensions,
                            "usage": safe_usage(result.get("usage")),
                            "estimated_yuan": settings.package_generation_estimated_cost_yuan,
                        }
                    )
                if status in {"completed", "failed"}:
                    async with AsyncSessionLocal() as db:
                        row = await db.get(ProviderCall, call_id)
                        row.status = (
                            "received" if status == "completed" else "provider_failed"
                        )
                        row.details = {
                            **row.details,
                            "async_status": status,
                            "usage": report.get("usage"),
                            "estimated_yuan": report.get("estimated_yuan"),
                        }
                        await db.commit()
                    (output / "report.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2)
                    )
                    print(
                        json.dumps(
                            {
                                k: v
                                for k, v in report.items()
                                if k not in {"transport", "input"}
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    break
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                print(
                    json.dumps(
                        {"poll_error": type(exc).__name__, "action": "retry GET only"}
                    ),
                    flush=True,
                )
            await asyncio.sleep(10)
        else:
            print(
                json.dumps(
                    {"status": "still_processing_or_unreachable", "task_id": task_id}
                ),
                flush=True,
            )
    await engine.dispose()


if __name__ == "__main__":
    import sys

    asyncio.run(main(sys.argv[1]))
