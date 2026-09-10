"""Fail closed before calls; retain usage even when business transactions fail."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.models.provider_operation import PlanningOperation, ProviderCall
from app.schemas.creative_plan import CreativePlan
from app.services.generation_accounting import safe_usage

_scope = ContextVar("provider_call_scope", default=None)


@contextmanager
def call_scope(factory, user_id, stage, **identity):
    token = _scope.set((factory, user_id, stage, identity))
    try:
        yield
    finally:
        _scope.reset(token)


async def plan_once(db, user_id, key, signature, invoke):
    """A crashed/failed claim is not stolen: a new explicit key is required."""
    from app.services.generation_service import GenerationDomainError

    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    key = key or str(uuid4())
    operation_id = uuid4()
    async with factory() as ledger:
        claimed = (
            await ledger.execute(
                insert(PlanningOperation)
                .values(
                    id=operation_id,
                    user_id=user_id,
                    request_key=key,
                    signature=signature,
                    status="invoking",
                )
                .on_conflict_do_nothing()
                .returning(PlanningOperation.id)
            )
        ).scalar_one_or_none()
        await ledger.commit()
    if not claimed:
        for _ in range(125):  # Bounded wait for the winner; no model retry.
            async with factory() as ledger:
                row = (
                    await ledger.execute(
                        select(PlanningOperation).where(
                            PlanningOperation.user_id == user_id,
                            PlanningOperation.request_key == key,
                        )
                    )
                ).scalar_one()
                if row.signature != signature:
                    raise GenerationDomainError(
                        "idempotency_conflict",
                        "请求内容已变化，请使用新的任务标识",
                        409,
                    )
                if row.status == "succeeded":
                    return CreativePlan.model_validate(row.result), str(row.id)
                if (
                    row.status != "invoking"
                    or (datetime.now(timezone.utc) - row.created_at).total_seconds()
                    > 65
                ):
                    raise GenerationDomainError(
                        "planning_outcome_unknown",
                        "此前规划未完成，费用需核实；不会自动重试",
                        409,
                    )
            await asyncio.sleep(0.5)
        raise GenerationDomainError(
            "planning_in_progress", "方案正在准备，请稍后查询相同请求", 409
        )
    try:
        with call_scope(factory, user_id, "planning", planning_id=operation_id):
            result = await invoke()
        async with factory() as ledger:
            row = await ledger.get(PlanningOperation, operation_id)
            row.status, row.result = "succeeded", result.model_dump()
            await ledger.commit()
        return result, str(operation_id)
    except BaseException:
        # Cancellation/process death can leave invoking; it is never automatically reclaimed.
        async with factory() as ledger:
            row = await ledger.get(PlanningOperation, operation_id)
            row.status = "failed"
            await ledger.commit()
        raise


def price_snapshot(stage, model):
    if stage == "generation":
        return {
            "basis": "configured_per_image",
            "currency": "CNY",
            "unit_yuan": settings.package_generation_estimated_cost_yuan,
            "version": settings.provider_price_version,
        }
    return {
        "basis": "configured_tokens",
        "currency": "CNY",
        "model": model,
        "input_per_million": settings.vl_input_yuan_per_million,
        "output_per_million": settings.vl_output_yuan_per_million,
        "version": settings.provider_price_version,
    }


async def recorded_post(client, url, *, model, **kwargs):
    scope = _scope.get()
    if scope is None:
        return await client.post(url, **kwargs)
    factory, user_id, stage, identity = scope
    details = {
        "provider_origin": f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
        "requested_model": model,
        "price": price_snapshot(stage, model),
        "actual_yuan": None,
        "estimated_yuan": None,
        "usage": None,
        "billing_status": "unreconciled",
    }
    call_id = uuid4()
    async with factory() as db:
        db.add(
            ProviderCall(
                id=call_id, user_id=user_id, stage=stage, details=details, **identity
            )
        )
        await db.commit()  # Ledger failure prevents sending a paid request.
    started = perf_counter()
    status = "outcome_unknown"
    try:
        response = await client.post(url, **kwargs)
        response.extensions["provider_call_id"] = str(call_id)
        details["http_status"] = response.status_code
        status = "received" if response.status_code == 200 else "http_error"
        if response.status_code == 202:
            status = "accepted"
        try:
            body = response.json()
            if isinstance(body, dict):
                details["usage"] = safe_usage(body.get("usage"))
                task_id = body.get("task_id") or body.get("id")
                if (
                    response.status_code == 202
                    and isinstance(task_id, str)
                    and len(task_id) <= 128
                ):
                    details["async_task_id"] = task_id
        except ValueError:
            pass
        price, usage = details["price"], details["usage"]
        if response.status_code == 200 and stage == "generation":
            details["estimated_yuan"] = price["unit_yuan"]
        elif (
            usage
            and all(k in usage for k in ("input_tokens", "output_tokens"))
            and all(
                price[k] is not None
                for k in ("input_per_million", "output_per_million")
            )
        ):
            details["estimated_yuan"] = float(
                (
                    Decimal(str(price["input_per_million"])) * usage["input_tokens"]
                    + Decimal(str(price["output_per_million"])) * usage["output_tokens"]
                )
                / 1_000_000
            )
        return response
    except BaseException as exc:
        from app.services.generation_errors import failure_details

        details["failure"], _ = failure_details(exc)
        raise
    finally:
        details["latency_ms"] = round((perf_counter() - started) * 1000, 3)
        async with factory() as db:
            row = await db.get(ProviderCall, call_id)
            row.status, row.details = status, details
            await db.commit()


def summarize_calls(rows):
    known = [r.details.get("estimated_yuan") for r in rows]
    return {
        "currency": "CNY",
        "scope": "generation_workflow_recorded_calls",
        "call_count": len(rows),
        "estimated_yuan_known": float(
            sum(Decimal(str(x)) for x in known if x is not None)
        ),
        "unknown_estimate_count": sum(x is None for x in known),
        "actual_yuan": None,
        "complete": False,
        "billing_status": "unreconciled",
        "calls": [
            {"id": str(r.id), "stage": r.stage, "status": r.status, **r.details}
            for r in rows
        ],
    }
