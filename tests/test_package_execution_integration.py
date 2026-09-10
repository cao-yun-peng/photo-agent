"""Real PG + ARQ queue + worker; model/OSS remain explicit local fixtures."""

# ruff: noqa: F811
import asyncio
import hashlib
import os
from types import SimpleNamespace
from app.core.security import get_current_user
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from app.core.telemetry import traced_job
from sqlalchemy import select, update

from app.api.generations import router as generations_router
from app.config import settings
from app.models.generation import Generation
from app.models.photo import Photo
from app.models.rate_limit import RateLimit
from app.models.skill import Skill
from app.services import oss
from app.services.generation_service import (
    prepare_generation,
    confirm_generation,
    GenerationDomainError,
)
from app.schemas.creative_plan import PackageOptions
from app.workers import gen_tasks, tasks
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_skill_package import archive, client_app
from tests.test_package_execution import png

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]


async def prepare_fixture(factory, user, monkeypatch, tmp_path):
    monkeypatch.setattr(oss, "_MOCK_ROOT", tmp_path / "oss")
    monkeypatch.setattr(gen_tasks, "AsyncSessionLocal", factory)
    monkeypatch.setattr(gen_tasks, "log_event", AsyncMock())
    pid = uuid4()
    image = png()
    key = f"photos/{user}/{pid}.png"
    await oss.put_object(key, image, content_type="image/png")
    package = archive(
        {
            "SKILL.md": "---\nname: knit\ndescription: knit poster\n---\nUse `assets/style.png` as style only. Keep source orientation.",
            "assets/style.png": png(color="blue"),
        }
    )
    async with factory() as db:
        db.add(
            Photo(
                id=pid,
                user_id=user,
                hash=hashlib.sha256(image).hexdigest(),
                oss_key=key,
                width=48,
                height=32,
                size_bytes=len(image),
                mime_type="image/png",
                status="done",
            )
        )
        await db.commit()
        app = client_app(db, user)
        app.include_router(generations_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            preview = (
                await client.post("/skills/packages/preview", content=package)
            ).json()
            imported = await client.post(
                "/skills/packages/import",
                content=package,
                params={"expected_hash": preview["content_sha256"]},
            )
            assert imported.status_code == 200
            sid = UUID(imported.json()["skill_id"])
            response = await client.post(
                f"/photos/{pid}/generate",
                json={
                    "skill_id": str(sid),
                    "extra_prompt": "不要文字",
                    "idempotency_key": "test-package-" + pid.hex,
                },
            )
            assert response.status_code == 202, response.text
            generation = response.json()
            assert (
                generation["status"] == "awaiting_confirmation"
            )  # Even control variant.
            assert generation["execution_snapshot"]["plan"]["title"] == ""
            frozen = await client.get(f"/generations/{generation['id']}/inputs/0")
            assert (
                frozen.status_code == 200
                and frozen.headers["content-type"] == "image/png"
            )
            assert frozen.content == image
            denied = await client.post(
                f"/generations/{generation['id']}/confirm",
                json={
                    "confirmation_token": generation["confirmation_token"],
                    "execution_digest": "0" * 64,
                },
            )
            assert denied.status_code == 409
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=uuid4()
            )
            assert (
                await client.get(f"/generations/{generation['id']}/inputs/0")
            ).status_code == 404

    return pid, sid, generation


@pytest.mark.asyncio
async def test_real_queue_confirm_replay_and_frozen_inputs(
    infra, monkeypatch, tmp_path
):  # noqa: F811
    factory, _, user = infra
    pid, sid, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    gid, token = UUID(data["id"]), UUID(data["confirmation_token"])
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    monkeypatch.setattr(tasks, "_pool", pool)
    # Updating/deleting the Skill cannot change already-frozen instructions/assets.
    async with factory() as db:
        await db.execute(
            update(Skill)
            .where(Skill.id == sid)
            .values(
                name="new name",
                prompt_template="must not use",
                current_version_id=uuid4(),
            )
        )
        await db.commit()

    async def confirm_once():
        async with factory() as db:
            return await confirm_generation(
                db=db,
                user_id=user,
                generation_id=gid,
                confirmation_token=token,
                execution_digest=data["execution_digest"],
            )

    try:
        await asyncio.gather(confirm_once(), confirm_once())
        worker = Worker(
            [traced_job(gen_tasks.generate_photo)],
            redis_pool=pool,
            burst=True,
            handle_signals=False,
            max_jobs=1,
        )
        await worker.async_run()
        async with factory() as db:
            generation = await db.get(Generation, gid)
            assert generation.status == "done"
            assert generation.execution_snapshot["skill_name"] == "knit"
            assert generation.execution_snapshot["inputs"][0]["role"] == "subject"
            assert generation.execution_snapshot["inputs"][1]["role"] == "style"
            assert generation.verification["simulated"]
            assert generation.verification["status"] == "needs_review"
            assert generation.result_oss_key != (await db.get(Photo, pid)).oss_key
            assert await oss.get_object(generation.result_oss_key)
            quota = (
                await db.execute(select(RateLimit).where(RateLimit.user_id == user))
            ).scalar_one()
            assert quota.gen_count == 1 and quota.gen_reserved == 0
        await confirm_once()
        assert (await gen_tasks.generate_photo({}, str(gid)))[
            "reason"
        ] == "already_done"
    finally:
        await pool.aclose()


@pytest.mark.asyncio
async def test_revised_plan_invalidates_old_confirmation_and_tamper_is_rejected(
    infra, monkeypatch, tmp_path
):  # noqa: F811
    factory, _, user = infra
    pid, sid, old = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    async with factory() as db:
        fresh = await prepare_generation(
            db=db,
            user_id=user,
            photo_id=pid,
            skill_id=sid,
            extra_prompt="换成简洁风格",
            package_options=PackageOptions(title_mode="exact", title="山间"),
            idempotency_key="fresh-" + pid.hex,
        )
        fresh_id, fresh_token, fresh_digest = (
            fresh.id,
            fresh.confirmation_token,
            fresh.execution_digest,
        )
    async with factory() as db:
        with pytest.raises(GenerationDomainError, match="失效"):
            await confirm_generation(
                db=db,
                user_id=user,
                generation_id=UUID(old["id"]),
                confirmation_token=UUID(old["confirmation_token"]),
                execution_digest=old["execution_digest"],
            )
    async with factory() as db:
        await db.execute(
            update(Generation)
            .where(Generation.id == fresh_id)
            .values(estimated_cost_yuan=999)
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(GenerationDomainError, match="费用"):
            await confirm_generation(
                db=db,
                user_id=user,
                generation_id=fresh_id,
                confirmation_token=fresh_token,
                execution_digest=fresh_digest,
            )


@pytest.mark.asyncio
async def test_provider_failure_never_repeats_package_call_and_releases_reservation(
    infra, monkeypatch, tmp_path
):  # noqa: F811
    factory, _, user = infra
    _, _, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    gid = UUID(data["id"])
    monkeypatch.setattr(tasks, "enqueue_generate_photo", AsyncMock(return_value=True))
    async with factory() as db:
        await confirm_generation(
            db=db,
            user_id=user,
            generation_id=gid,
            confirmation_token=UUID(data["confirmation_token"]),
            execution_digest=data["execution_digest"],
        )
    provider = AsyncMock(side_effect=TimeoutError("ambiguous provider timeout"))
    monkeypatch.setattr(gen_tasks.image_gen, "generate", provider)
    result = await gen_tasks.generate_photo({}, str(gid))
    assert not result["ok"]
    assert (await gen_tasks.generate_photo({}, str(gid)))[
        "reason"
    ] == "package_attempt_already_consumed"
    provider.assert_awaited_once()
    async with factory() as db:
        quota = (
            await db.execute(select(RateLimit).where(RateLimit.user_id == user))
        ).scalar_one()
        assert quota.gen_reserved == 0


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("PHOTO_AGENT_KNIT_SAMPLE_DIR"), reason="external knit sample not configured"
)
async def test_external_knit_package_execution(infra, monkeypatch, tmp_path):
    """Opt-in real user package; original files are read-only, providers are mocked."""
    from pathlib import Path
    import io
    import zipfile
    import sys

    sample = Path(os.environ["PHOTO_AGENT_KNIT_SAMPLE_DIR"]).resolve()
    assert (sample / "SKILL.md").is_file()
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive_file:
        for asset in sorted(sample.rglob("*")):
            if asset.is_file():
                archive_file.write(asset, asset.relative_to(sample).as_posix())
    monkeypatch.setattr(sys.modules[__name__], "archive", lambda _: bundle.getvalue())
    factory, _, user = infra
    _, _, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    snapshot = data["execution_snapshot"]
    assert snapshot["inputs"][0]["role"] == "subject"
    assert any(item["role"] == "style" for item in snapshot["inputs"])
    monkeypatch.setattr(tasks, "enqueue_generate_photo", AsyncMock(return_value=True))
    async with factory() as db:
        await confirm_generation(
            db=db, user_id=user, generation_id=UUID(data["id"]),
            confirmation_token=UUID(data["confirmation_token"]),
            execution_digest=data["execution_digest"],
        )
    result = await gen_tasks.generate_photo({}, data["id"])
    assert result["ok"], result
    async with factory() as db:
        generation = await db.get(Generation, UUID(data["id"]))
        assert generation.status == "done"
        assert generation.verification["simulated"]
