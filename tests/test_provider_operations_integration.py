# ruff: noqa: F811
"""Durable claim races, failure accounting, ownership and real worker composition."""

import asyncio
import base64
import json
import os
from uuid import UUID, uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import select
from app.models.provider_operation import PlanningOperation, ProviderCall
from app.services.provider_ledger import plan_once, recorded_post
from app.services.generation_service import (
    GenerationDomainError,
    prepare_generation,
    confirm_generation,
)
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_package_execution import plan, png

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated DB not configured",
    ),
]


@pytest.mark.asyncio
async def test_concurrent_claim_calls_once_and_conflicting_key_never_calls(infra):
    factory, _, user = infra
    count = 0

    async def invoke():
        nonlocal count
        count += 1
        await asyncio.sleep(0.1)
        return plan()

    async def request(signature="a" * 64):
        async with factory() as db:
            return await plan_once(db, user, "same-key", signature, invoke)

    results = await asyncio.gather(request(), request())
    assert count == 1 and results[0][1] == results[1][1]
    with pytest.raises(GenerationDomainError) as error:
        await request("b" * 64)
    assert error.value.code == "idempotency_conflict" and count == 1


@pytest.mark.asyncio
async def test_failure_is_durable_and_not_paid_again(infra):
    factory, _, user = infra
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("secret provider body must not be persisted")

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await recorded_post(
                client, "https://provider.example/v1", model="qwen-vl-plus"
            )

    async with factory() as db:
        with pytest.raises(httpx.ReadTimeout):
            await plan_once(db, user, "failed", "a" * 64, invoke)
    async with factory() as db:
        with pytest.raises(GenerationDomainError):
            await plan_once(db, user, "failed", "a" * 64, invoke)
        rows = (
            (await db.execute(select(ProviderCall).where(ProviderCall.user_id == user)))
            .scalars()
            .all()
        )
        assert count == 1 and len(rows) == 1
        assert rows[0].status == "outcome_unknown"
        assert rows[0].details["actual_yuan"] is None
        assert "secret" not in json.dumps(rows[0].details)


@pytest.mark.asyncio
async def test_usage_survives_invalid_plan_and_business_rollback(infra):
    factory, _, user = infra

    async def invoke():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": 2,
                            "secret": "drop",
                        }
                    },
                )
            )
        ) as client:
            await recorded_post(
                client, "https://provider.example/v1", model="qwen-vl-plus"
            )
        raise ValueError("invalid plan")

    async with factory() as db:
        with pytest.raises(ValueError):
            await plan_once(db, user, "invalid-plan", "a" * 64, invoke)
        await db.rollback()
    async with factory() as db:
        call = (
            await db.execute(select(ProviderCall).where(ProviderCall.user_id == user))
        ).scalar_one()
        assert call.status == "received" and call.details["usage"] == {
            "input_tokens": 5,
            "output_tokens": 2,
        }
        op = (
            await db.execute(
                select(PlanningOperation).where(PlanningOperation.user_id == user)
            )
        ).scalar_one()
        assert op.status == "failed"


@pytest.mark.asyncio
async def test_three_stage_ledger_and_cost_api_ownership(infra, monkeypatch, tmp_path):
    from tests.test_package_execution_integration import prepare_fixture
    from app.services import package_execution as execution, image_gen
    from app.workers import tasks, gen_tasks
    from app.config import settings
    from app.api.generations import generation_call_cost
    from fastapi import HTTPException

    factory, _, user = infra
    pid, sid, _ = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    original = httpx.AsyncClient

    async def handler(request):
        if request.url.path.endswith("/images/edits"):
            body = {
                "data": [{"b64_json": base64.b64encode(png((1536, 1024))).decode()}],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            }
        else:
            payload = json.loads(request.content)
            text = payload["input"]["messages"][0]["content"][-1]["text"]
            result = (
                plan("").model_dump()
                if "修图创作规划器" in text
                else dict(
                    subject_preserved=True,
                    style_aligned=True,
                    title_correct=True,
                    no_reference_copy=True,
                    issues=[],
                )
            )
            body = {
                "output": {
                    "choices": [
                        {"message": {"content": [{"text": json.dumps(result)}]}}
                    ]
                },
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(execution, "_is_mock", lambda: False)
    monkeypatch.setattr(image_gen, "_is_openai_mock", lambda: False)
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "vl_input_yuan_per_million", 0.8)
    monkeypatch.setattr(settings, "vl_output_yuan_per_million", 2)
    monkeypatch.setattr(tasks, "enqueue_generate_photo", AsyncMock(return_value=True))
    async with factory() as db:
        gen = await prepare_generation(
            db=db,
            user_id=user,
            photo_id=UUID(str(pid)),
            skill_id=UUID(str(sid)),
            extra_prompt="不要任何文字",
            idempotency_key="ledger-flow",
        )
        gid = gen.id
        await confirm_generation(
            db=db,
            user_id=user,
            generation_id=gid,
            confirmation_token=gen.confirmation_token,
            execution_digest=gen.execution_digest,
        )
    assert (await gen_tasks.generate_photo({}, str(gid)))["ok"]
    async with factory() as db:
        result = await generation_call_cost(gid, SimpleNamespace(id=user), db)
        assert [r["stage"] for r in result["calls"]] == [
            "planning",
            "generation",
            "verification",
        ]
        assert result["unknown_estimate_count"] == 0 and result["actual_yuan"] is None
        with pytest.raises(HTTPException) as denied:
            await generation_call_cost(gid, SimpleNamespace(id=uuid4()), db)
        assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_provider_price_change_invalidates_confirmation(
    infra, monkeypatch, tmp_path
):
    from tests.test_package_execution_integration import prepare_fixture
    from app.config import settings

    factory, _, user = infra
    _, _, data = await prepare_fixture(factory, user, monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "package_generation_estimated_cost_yuan", 9.0)
    async with factory() as db:
        with pytest.raises(GenerationDomainError) as error:
            await confirm_generation(
                db=db,
                user_id=user,
                generation_id=UUID(data["id"]),
                confirmation_token=UUID(data["confirmation_token"]),
                execution_digest=data["execution_digest"],
            )
        assert error.value.code == "snapshot_changed"
