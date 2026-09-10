"""P5 real PostgreSQL races; all provider calls are deterministic fixtures."""

# ruff: noqa: F811
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select, update
from app.config import settings
from app.models.generation import Generation
from app.models.rate_limit import RateLimit
from app.services.generation_service import confirm_generation, GenerationDomainError
from app.services.generation_lifecycle import (
    cancel_generation,
    prepare_iteration,
    recover_generations,
)
from app.workers import gen_tasks, tasks
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_package_execution_integration import prepare_fixture
from tests.test_package_execution import png

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated DB not configured",
    ),
]


async def confirmed(factory, user, monkeypatch, tmp_path):
    _, _, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    monkeypatch.setattr(tasks, "enqueue_generate_photo", AsyncMock(return_value=True))
    async with factory() as db:
        await confirm_generation(
            db=db,
            user_id=user,
            generation_id=UUID(data["id"]),
            confirmation_token=UUID(data["confirmation_token"]),
            execution_digest=data["execution_digest"],
        )
    return UUID(data["id"]), data


@pytest.mark.asyncio
async def test_cancel_before_worker_is_owned_idempotent_and_releases_quota(
    infra, monkeypatch, tmp_path
):  # noqa: F811
    factory, _, user = infra
    gid, data = await confirmed(factory, user, monkeypatch, tmp_path)
    async with factory() as db:
        with pytest.raises(GenerationDomainError) as denied:
            await cancel_generation(db, uuid4(), gid)
        assert denied.value.status_code == 404
    async with factory() as db:
        assert (await cancel_generation(db, user, gid)).status == "cancelled"
        assert (await cancel_generation(db, user, gid)).status == "cancelled"
    provider = AsyncMock()
    monkeypatch.setattr(gen_tasks.image_gen, "generate", provider)
    assert not (await gen_tasks.generate_photo({}, str(gid)))["ok"]
    provider.assert_not_awaited()
    async with factory() as db:
        with pytest.raises(GenerationDomainError):
            await confirm_generation(
                db=db,
                user_id=user,
                generation_id=gid,
                confirmation_token=UUID(data["confirmation_token"]),
                execution_digest=data["execution_digest"],
            )
    async with factory() as db:
        quota = (
            await db.execute(select(RateLimit).where(RateLimit.user_id == user))
        ).scalar_one()
        assert quota.gen_reserved == quota.gen_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_inflight_cancel_or_recovery_fences_late_results(
    infra, monkeypatch, tmp_path, recover
):  # noqa: F811
    factory, _, user = infra
    gid, _ = await confirmed(factory, user, monkeypatch, tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        entered.set()
        await finish.wait()
        return SimpleNamespace(
            image_bytes=png(), content_type="image/png", cost_yuan=0.3, model="mock"
        )

    call = AsyncMock(side_effect=provider)
    monkeypatch.setattr(gen_tasks.image_gen, "generate", call)
    worker = asyncio.create_task(gen_tasks.generate_photo({}, str(gid)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as db:
            if recover:
                await db.execute(
                    update(Generation)
                    .where(Generation.id == gid)
                    .values(
                        lease_expires_at=datetime.now(timezone.utc)
                        - timedelta(seconds=1)
                    )
                )
                await db.commit()
                assert await recover_generations(db) == 1
            else:
                assert (
                    await cancel_generation(db, user, gid)
                ).status == "cancel_requested"
        finish.set()
        result = await asyncio.wait_for(worker, 10)
        assert not result["ok"]
        async with factory() as db:
            gen = await db.get(Generation, gid)
            assert gen.status == ("outcome_unknown" if recover else "cancelled")
            assert not gen.result_oss_key and not gen.quota_reserved
            if not recover:
                assert gen.cost_yuan == Decimal("0.3")
            quota = (
                await db.execute(select(RateLimit).where(RateLimit.user_id == user))
            ).scalar_one()
            assert quota.gen_count == (0 if recover else 1)
        assert not (await gen_tasks.generate_photo({}, str(gid)))["ok"]
        call.assert_awaited_once()
    finally:
        finish.set()
        await worker


@pytest.mark.asyncio
async def test_iteration_reconfirms_same_frozen_inputs_and_enforces_limits(
    infra, monkeypatch, tmp_path
):  # noqa: F811
    factory, _, user = infra
    gid, _ = await confirmed(factory, user, monkeypatch, tmp_path)
    assert (await gen_tasks.generate_photo({}, str(gid)))["ok"]
    async with factory() as db:
        first = await prepare_iteration(
            db, user, gid, "增加留白，不要文字", "iterate-first"
        )
        first_id = first.id
        assert first.status == "awaiting_confirmation" and first.iteration_index == 1
        assert first.parent_generation_id == gid and first.root_generation_id == gid
        assert first.execution_snapshot["plan"]["title"] == ""
        root = await db.get(Generation, gid)
        assert first.execution_snapshot["inputs"] == root.execution_snapshot["inputs"]
        assert first.execution_digest != root.execution_digest
        assert (
            await prepare_iteration(
                db, user, gid, "增加留白，不要文字", "iterate-first"
            )
        ).id == first_id
        with pytest.raises(GenerationDomainError, match="已有调整"):
            await prepare_iteration(db, user, gid, "另一种", "iterate-second")
    async with factory() as db:
        first = await db.get(Generation, first_id)
        await confirm_generation(
            db=db,
            user_id=user,
            generation_id=first.id,
            confirmation_token=first.confirmation_token,
            execution_digest=first.execution_digest,
        )
    assert (await gen_tasks.generate_photo({}, str(first_id)))["ok"]
    async with factory() as db:
        monkeypatch.setattr(settings, "generation_chain_estimate_limit_yuan", 0.7)
        with pytest.raises(GenerationDomainError, match="费用估算"):
            await prepare_iteration(db, user, first_id, "更柔和", "iterate-budget")
    monkeypatch.setattr(settings, "generation_chain_estimate_limit_yuan", 1.0)
    async with factory() as db:
        second = await prepare_iteration(db, user, first_id, "更柔和", "iterate-second")
        assert second.iteration_index == 2
        await cancel_generation(db, user, second.id)
    async with factory() as db:
        with pytest.raises(GenerationDomainError, match="次数"):
            await prepare_iteration(db, user, first_id, "继续修改", "iterate-third")


@pytest.mark.asyncio
async def test_recovery_expires_unconfirmed_and_recovers_queue_without_provider(
    infra, monkeypatch, tmp_path
):  # noqa: F811
    factory, _, user = infra
    _, _, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    gid = UUID(data["id"])
    async with factory() as db:
        await db.execute(
            update(Generation)
            .where(Generation.id == gid)
            .values(
                confirmation_expires_at=datetime.now(timezone.utc)
                - timedelta(seconds=1)
            )
        )
        await db.commit()
        assert await recover_generations(db) == 1
        assert (await db.get(Generation, gid)).status == "expired"


@pytest.mark.asyncio
async def test_recovery_queue_without_provider(infra, monkeypatch, tmp_path):
    factory, _, user = infra
    gid, _ = await confirmed(factory, user, monkeypatch, tmp_path)
    async with factory() as db:
        await db.execute(
            update(Generation)
            .where(Generation.id == gid)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await db.commit()
        assert await recover_generations(db) == 1
        gen = await db.get(Generation, gid)
        assert (
            gen.status == "queue_failed"
            and gen.quota_reserved
            and not gen.attempt_count
        )
        await cancel_generation(db, user, gid)


@pytest.mark.asyncio
async def test_confirm_cancel_race_and_iteration_idempotency(
    infra, monkeypatch, tmp_path
):
    factory, _, user = infra
    _, _, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    gid = UUID(data["id"])
    monkeypatch.setattr(tasks, "enqueue_generate_photo", AsyncMock(return_value=True))

    async def confirm():
        async with factory() as db:
            try:
                await confirm_generation(
                    db=db,
                    user_id=user,
                    generation_id=gid,
                    confirmation_token=UUID(data["confirmation_token"]),
                    execution_digest=data["execution_digest"],
                )
            except GenerationDomainError as exc:
                assert exc.code == "confirmation_invalid"

    async def cancel():
        async with factory() as db:
            await cancel_generation(db, user, gid)

    await asyncio.gather(confirm(), cancel())
    async with factory() as db:
        gen = await db.get(Generation, gid)
        assert gen.status == "cancelled" and not gen.quota_reserved


@pytest.mark.asyncio
async def test_concurrent_iteration_keeps_one_plan(infra, monkeypatch, tmp_path):
    factory, _, user = infra
    gid, _ = await confirmed(factory, user, monkeypatch, tmp_path)
    assert (await gen_tasks.generate_photo({}, str(gid)))["ok"]

    async def prepare():
        async with factory() as db:
            gen = await prepare_iteration(
                db, user, gid, "留白更多", "concurrent-iteration"
            )
            return gen.id

    results = await asyncio.gather(prepare(), prepare())
    assert results[0] == results[1]


@pytest.mark.asyncio
async def test_owned_iteration_and_cancel_http_contract(infra, monkeypatch, tmp_path):
    import httpx
    from tests.test_skill_package import client_app
    from app.api.generations import router

    factory, _, user = infra
    gid, _ = await confirmed(factory, user, monkeypatch, tmp_path)
    assert (await gen_tasks.generate_photo({}, str(gid)))["ok"]
    async with factory() as db:
        app = client_app(db, user)
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/generations/{gid}/iterations",
                json={"feedback": "增加留白", "idempotency_key": "http-iteration-key"},
            )
            assert response.status_code == 202, response.text
            item = response.json()
            assert item["status"] == item["progress_stage"] == "awaiting_confirmation"
            assert item["parent_generation_id"] == str(gid)
            assert item["execution_digest"] and item["confirmation_token"]
            cancelled = await client.post(f"/generations/{item['id']}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["confirmation_token"] is None
