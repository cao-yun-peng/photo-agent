"""Local TCP + real auth/PG/Redis/ARQ, explicitly mocked model and OSS.

No HTTP handler/auth dependency override. Only infrastructure destinations change.
"""

# ruff: noqa: F811
import asyncio
import hashlib
import io
from PIL import Image
import json
import os
from pathlib import Path
import socket
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4
import httpx
import pytest
import uvicorn
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from sqlalchemy import delete
from app.config import settings
from app.core.security import create_access_token
from app.evaluation.release import latency_summary
from app.models.user import User
from app.services import oss
from app.workers import tasks, gen_tasks
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_package_execution import png
from tests.test_skill_package import archive

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"), reason="isolated DB required"
    ),
]


@pytest.mark.asyncio
async def test_real_http_upload_sse_confirm_worker_workspace(
    infra, monkeypatch, tmp_path
):
    factory, _, user = infra
    assert not settings.openai_api_key and not settings.dashscope_api_key
    monkeypatch.setattr(oss, "_MOCK_ROOT", tmp_path / "oss")
    monkeypatch.setattr(gen_tasks, "AsyncSessionLocal", factory)
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    monkeypatch.setattr(tasks, "_pool", pool)
    from app.main import app

    assert not app.dependency_overrides
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    second = uuid4()
    async with factory() as db:
        db.add(User(id=second, wechat_openid=f"p7-{second}"))
        await db.commit()
    timings = {}

    async def work():
        worker = Worker(
            tasks.WorkerSettings.functions,
            redis_pool=pool,
            burst=True,
            handle_signals=False,
            max_jobs=1,
            job_timeout=60,
        )
        await asyncio.wait_for(worker.async_run(), 75)
        assert worker.jobs_failed == 0

    try:
        for _ in range(200):
            if server.started:
                break
            if serving.done():
                await serving
            await asyncio.sleep(0.05)
        assert server.started
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=60,
            headers={"Authorization": f"Bearer {create_access_token(user)}"},
        ) as client:
            # API errors use the legacy envelope; assert absence of private content too.
            denied = await client.get(
                "/workspace", headers={"Authorization": "Bearer invalid"}
            )
            assert "revision" not in denied.json()
            picture = Image.new("RGB", (128, 96))
            picture.putdata(
                [
                    (x % 256, y * 2 % 256, (x + y) % 256)
                    for y in range(96)
                    for x in range(128)
                ]
            )
            encoded = io.BytesIO()
            picture.save(encoded, format="PNG")
            content = encoded.getvalue()
            photo_hash = hashlib.sha256(content).hexdigest()
            payload = {
                "hash": photo_hash,
                "size_bytes": len(content),
                "mime_type": "image/png",
            }
            signed = await client.post("/photos/upload-url", json=payload)
            assert signed.status_code == 200, signed.text
            signed = signed.json()
            url = urlsplit(signed["upload_url"])
            uploaded = await client.put(
                url.path + "?" + url.query, content=content, headers=signed["headers"]
            )
            assert uploaded.status_code in (200, 204), uploaded.text
            created = await client.post(
                "/photos", json={**payload, "oss_key": signed["oss_key"]}
            )
            assert created.status_code == 201, created.text
            pid = created.json()["id"]
            await work()
            photo = await client.get(f"/photos/{pid}")
            assert photo.json()["status"] == "partial_done", photo.text
            assert photo.json()["search_index_status"] == "ready"
            started = perf_counter()
            events = []
            async with client.stream(
                "POST", "/agent/stream", json={"query": "你好"}
            ) as stream:
                assert stream.status_code == 200
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        if not events:
                            timings["sse_first_event_ms"] = (
                                perf_counter() - started
                            ) * 1000
                        events.append(json.loads(line[6:]))
            assert events[-1]["type"] == "done", events
            timings["sse_total_ms"] = (perf_counter() - started) * 1000
            package = archive(
                {
                    "SKILL.md": "---\nname: knit\ndescription: knit poster\n---\nUse `assets/style.png` as style only.",
                    "assets/style.png": png(color="blue"),
                }
            )
            preview = await client.post("/skills/packages/preview", content=package)
            assert preview.status_code == 200, preview.text
            imported = await client.post(
                "/skills/packages/import",
                content=package,
                params={"expected_hash": preview.json()["content_sha256"]},
            )
            assert imported.status_code == 200, imported.text
            sid = imported.json()["skill_id"]
            plan = await client.post(
                f"/photos/{pid}/generate",
                json={
                    "skill_id": sid,
                    "extra_prompt": "不要文字",
                    "idempotency_key": uuid4().hex,
                },
            )
            assert plan.status_code == 202, plan.text
            data = plan.json()
            assert data["status"] == "awaiting_confirmation"
            gid = data["id"]
            foreign = await client.get(
                f"/generations/{gid}/inputs/0",
                headers={"Authorization": f"Bearer {create_access_token(second)}"},
            )
            assert foreign.status_code == 404
            confirmation = {
                "confirmation_token": data["confirmation_token"],
                "execution_digest": data["execution_digest"],
            }
            bad = await client.post(
                f"/generations/{gid}/confirm",
                json={**confirmation, "execution_digest": "0" * 64},
            )
            assert bad.status_code == 409
            started = perf_counter()
            for _ in range(2):
                confirmed = await client.post(
                    f"/generations/{gid}/confirm", json=confirmation
                )
                assert confirmed.status_code in (200, 202), confirmed.text
            await work()
            result = await client.get(f"/generations/{gid}")
            assert result.status_code == 200, result.text
            result = result.json()
            assert result["status"] == "done", result
            assert result["verification"]["simulated"]
            metrics = result["verification"]["execution_metrics"]
            assert metrics["cost"]["basis"] == "mock_zero"
            assert (
                metrics["cost"]["actual_yuan"] == 0 and not metrics["cost"]["complete"]
            )
            timings["confirm_to_result_ms"] = (perf_counter() - started) * 1000

            async def command(kind, revision, **fields):
                response = await client.post(
                    "/workspace/actions",
                    json={
                        "kind": kind,
                        "expected_revision": revision,
                        "idempotency_key": uuid4().hex,
                        **fields,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            await command("add_selection", 0, photo_ids=[pid])
            saved = await command("save_album", 1, title="P7 smoke")
            undone = await client.post(
                f"/workspace/actions/{saved['operation_id']}/undo",
                json={"expected_revision": 2},
            )
            assert undone.status_code == 200, undone.text
            samples = []
            for _ in range(20):
                started = perf_counter()
                fresh = await client.get("/workspace")
                assert fresh.status_code == 200
                assert (
                    len(fresh.json()["selection"]) == 1 and not fresh.json()["albums"]
                )
                samples.append((perf_counter() - started) * 1000)
            timings["workspace_read"] = latency_summary(samples)
            timings["workspace_samples_ms"] = samples
            report = {
                "mode": "mock",
                "transport": "TCP",
                "model_cost_actual_yuan": 0,
                "quality_measured": False,
                "production_slo_measured": False,
                "latency": timings,
                "worker_metrics": metrics,
            }
            if os.getenv("PHOTO_AGENT_P7_REPORT"):
                Path(os.environ["PHOTO_AGENT_P7_REPORT"]).write_text(
                    json.dumps(report, indent=2), encoding="utf-8"
                )
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
        sock.close()
        await pool.aclose()
        async with factory() as db:
            await db.execute(delete(User).where(User.id == second))
            await db.commit()
