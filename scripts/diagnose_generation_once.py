"""Replay one frozen generation with transport tracing and a separate billing ledger.

Run inside the existing API/Worker environment. Requires --execute-live, never retries.
Only sanitized metadata goes to stdout/report; image bytes stay in the output directory.
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database import AsyncSessionLocal, OwnedAsyncSession, engine
from app.models.generation import Generation
from app.models.provider_operation import ProviderCall
from app.services import image_gen, provider_ledger
from app.services.generation_errors import failure_details
from app.services.package_execution import validate_execution


async def main(args):
    engine.echo = False
    logging.disable(logging.CRITICAL)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)  # Refuse accidental replay.
    report = {
        "run_id": args.run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "live_post_limit": 1,
        "transport": [],
        "preflight": [],
    }
    async with AsyncSessionLocal() as db:
        previous = (
            await db.execute(
                select(ProviderCall.id).where(
                    ProviderCall.details["diagnostic_run_id"].astext == args.run_id
                )
            )
        ).first()
        if previous:
            raise RuntimeError("Diagnostic run already exists; refusing replay")
        generation = await db.get(Generation, UUID(args.generation_id))
        if generation is None or generation.model != "gpt-image-2":
            raise RuntimeError("Expected an existing gpt-image-2 generation")
        frozen = await validate_execution(db, generation)
        snapshot = generation.execution_snapshot
        user_id = generation.user_id
    if image_gen._is_openai_mock():
        raise RuntimeError(
            "Refusing mock: this diagnostic requires configured credentials"
        )
    if args.variant in {"minimal", "text"}:
        frozen = frozen[:1]
        snapshot = {
            **snapshot,
            "prompt": "Make the lighting slightly warmer. Preserve the scene.",
            "size": "1024x1024",
        }
        if args.variant == "text":
            snapshot["prompt"] = "A red ceramic cup on a plain white background."
    report["variant"] = args.variant
    report["quality_override"] = args.quality
    report["async_api"] = args.async_api
    report["input"] = {
        "count": len(frozen),
        "bytes": [len(b) for b in frozen],
        "dimensions": [list(Image.open(io.BytesIO(b)).size) for b in frozen],
        "prompt_chars": len(snapshot["prompt"]),
        "size": snapshot["size"],
        "snapshot_digest": generation.execution_digest,
    }
    report["timeout"] = {
        k: getattr(image_gen._GPT_TIMEOUT, k)
        for k in ("connect", "read", "write", "pool")
    }
    report["proxy_env_present"] = [
        k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if os.getenv(k)
    ]
    base = settings.openai_base_url.rstrip("/")
    report["provider_origin"] = "https://" + urlsplit(base).netloc
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
        started = perf_counter()
        try:
            response = await client.get(
                base + "/models",
                headers={"Authorization": "Bearer " + settings.openai_api_key},
            )
            report["preflight"].append(
                {
                    "route": "models",
                    "http_status": response.status_code,
                    "latency_ms": round((perf_counter() - started) * 1000, 3),
                }
            )
        except httpx.RequestError as exc:
            report["preflight"].append(
                {"route": "models", "exception_type": type(exc).__name__}
            )
    print(
        json.dumps(
            {
                "input": report["input"],
                "preflight": report["preflight"],
                "next": "one live image POST",
            }
        ),
        flush=True,
    )

    class DiagnosticSession(OwnedAsyncSession):
        def add(self, instance, **kwargs):
            if isinstance(instance, ProviderCall):
                instance.details.update(
                    {
                        "diagnostic_run_id": args.run_id,
                        "source_generation_id": args.generation_id,
                    }
                )
                report["provider_call_id"] = str(instance.id)
            return super().add(instance, **kwargs)

    factory = async_sessionmaker(
        engine, class_=DiagnosticSession, expire_on_commit=False
    )
    original_post = provider_ledger.recorded_post
    started = perf_counter()

    async def trace(name, info):
        event = {
            "event": name,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        }
        if "exception" in info:
            event["exception_type"] = type(info["exception"]).__name__
        report["transport"].append(event)
        print(json.dumps(event), flush=True)

    async def traced_post(client, url, **kwargs):
        if args.variant == "text":
            url = url.removesuffix("/edits") + "/generations"
            kwargs.pop("files")
            kwargs["json"] = kwargs.pop("data")
        if args.quality:
            kwargs.get("json", kwargs.get("data"))["quality"] = args.quality
        if args.async_api:
            url += "/async"
        kwargs["extensions"] = {"trace": trace}
        response = await original_post(client, url, **kwargs)
        report["http_status"] = response.status_code
        report["response_bytes"] = len(response.content)
        report["response_content_type"] = response.headers.get("content-type", "")
        if args.async_api and response.status_code == 202:
            task = response.json()
            task_id = task.get("task_id") or task.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("Missing async task ID")
            report["async_task_id"] = task_id
            async with factory() as db:
                row = await db.get(ProviderCall, UUID(report["provider_call_id"]))
                row.status = "processing"
                row.details = {**row.details, "async_task_id": task_id}
                await db.commit()
            (output / "report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            print(json.dumps({"async_task_id": task_id, "accepted": True}), flush=True)
            deadline = perf_counter() + 300
            previous_status = None
            while perf_counter() < deadline:
                await asyncio.sleep(5)
                polled = await client.get(
                    base + "/images/tasks/" + quote(task_id, safe=""),
                    headers={"Authorization": "Bearer " + settings.openai_api_key},
                )
                polled.raise_for_status()
                task = polled.json()
                state = task.get("status")
                if state != previous_status:
                    print(
                        json.dumps(
                            {
                                "async_status": state,
                                "elapsed_ms": round((perf_counter() - started) * 1000),
                            }
                        ),
                        flush=True,
                    )
                    previous_status = state
                report["async_status"] = state
                if state in {"completed", "failed"}:
                    async with factory() as db:
                        row = await db.get(
                            ProviderCall, UUID(report["provider_call_id"])
                        )
                        row.status = (
                            "received" if state == "completed" else "provider_failed"
                        )
                        row.details = {**row.details, "async_status": state}
                        await db.commit()
                    if state == "failed":
                        report["async_error_code"] = (task.get("error") or {}).get(
                            "code"
                        )
                        raise RuntimeError("Async provider task failed")
                    result_body = task.get("result") or {}
                    if not (result_body.get("data") or [{}])[0].get("b64_json"):
                        image_url = task.get("image_url")
                        parsed = urlsplit(image_url or "")
                        if (
                            parsed.scheme != "https"
                            or not parsed.hostname
                            or parsed.username
                            or parsed.password
                        ):
                            raise ValueError("Unsupported async image URL")
                        downloaded = await client.get(image_url)
                        downloaded.raise_for_status()
                        result_body = {
                            "data": [
                                {
                                    "b64_json": base64.b64encode(
                                        downloaded.content
                                    ).decode()
                                }
                            ]
                        }
                    return httpx.Response(200, json=result_body)
            raise TimeoutError("Async task still processing; do not resubmit")
        return response

    provider_ledger.recorded_post = traced_post
    try:
        with provider_ledger.call_scope(factory, user_id, "generation"):
            result = await image_gen.generate(
                "",
                snapshot["prompt"],
                model="gpt-image-2",
                image_inputs=frozen,
                size=snapshot["size"],
            )
        image = Image.open(io.BytesIO(result.image_bytes))
        image.verify()
        (output / "result.png").write_bytes(result.image_bytes)
        report.update(
            {
                "outcome": "image_received",
                "output_dimensions": list(image.size),
                "output_sha256": hashlib.sha256(result.image_bytes).hexdigest(),
                "estimated_yuan": result.cost_yuan,
                "usage": result.usage,
            }
        )
    except Exception as exc:
        report["outcome"] = "failed"
        report["failure"], _ = failure_details(exc)
    finally:
        provider_ledger.recorded_post = original_post
        report["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        report["actual_billing"] = "unreconciled"
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--execute-live", required=True, action="store_true")
    parser.add_argument(
        "--variant", choices=["original", "minimal", "text"], default="original"
    )
    parser.add_argument("--quality", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--async-api", action="store_true")
    asyncio.run(main(parser.parse_args()))
