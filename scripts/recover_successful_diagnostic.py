"""Import one already-generated diagnostic task as a separate owned recovery job.

Never submits an image request. Preserves the old failed task and links its separate
diagnostic billing entry to the new recovery record before GET-only finalization.
"""

import asyncio
import copy
import json
import logging
import time
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models.generation import Generation, GenerationInput
from app.models.provider_operation import ProviderCall
from app.models.user import User
from app.services.generation_service import _reserve_quota
from app.services.package_execution import (
    configured_image_contract,
    digest,
    validate_execution,
)


async def main():
    engine.echo = False
    logging.disable(logging.CRITICAL)
    async with AsyncSessionLocal() as db:
        source = await db.get(Generation, UUID("4422cca8-53ac-41dd-a60b-124ffce3506b"))
        await db.execute(
            select(User.id).where(User.id == source.user_id).with_for_update()
        )
        call = await db.get(ProviderCall, UUID("ad3806fc-710f-4797-a62e-e553e9a36607"))
        assert (
            call.user_id == source.user_id
            and call.details.get("diagnostic_run_id") == "gen-diag-20260910-async-04"
        )
        assert (
            call.status == "received"
            and call.details.get("async_status") == "completed"
        )
        if call.generation_id:
            gid = call.generation_id
        else:
            assert settings.openai_image_transport == "timicc_async"
            snapshot = copy.deepcopy(source.execution_snapshot)
            snapshot["supplier_contract"] = configured_image_contract()
            snapshot["recovery_source_generation_id"] = str(source.id)
            gid = uuid4()
            if not await _reserve_quota(db, source.user_id):
                raise RuntimeError(
                    "Daily quota unavailable; image remains safely saved locally"
                )
            row = Generation(
                id=gid,
                user_id=source.user_id,
                source_photo_id=source.source_photo_id,
                skill_id=source.skill_id,
                model=source.model,
                extra_prompt=source.extra_prompt,
                status="processing",
                progress_stage="async_waiting",
                enqueue_status="consumed",
                attempt_count=1,
                estimated_cost_yuan=source.estimated_cost_yuan,
                execution_snapshot=snapshot,
                execution_digest=digest(snapshot),
                idempotency_key="recovered-diagnostic-async-04",
                quota_reserved=True,
                quota_reserved_day=date.today(),
                verification={
                    "recovered_from_diagnostic": True,
                    "provider_task": {
                        "id": call.details["async_task_id"],
                        "call_id": str(call.id),
                        "base_url": settings.openai_base_url.rstrip("/"),
                        "deadline": time.time() + 1800,
                    },
                },
            )
            db.add(row)
            inputs = (
                (
                    await db.execute(
                        select(GenerationInput).where(
                            GenerationInput.generation_id == source.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for item in inputs:
                db.add(
                    GenerationInput(
                        generation_id=gid,
                        position=item.position,
                        content=item.content,
                        media_type=item.media_type,
                    )
                )
            call.generation_id = gid
            await db.flush()
            await validate_execution(db, row, check_source=False)
            await db.commit()
        print(
            json.dumps({"recovery_generation_id": str(gid), "image_submissions": 0}),
            flush=True,
        )
    previous = None
    for _ in range(72):
        async with AsyncSessionLocal() as db:
            row = await db.get(Generation, gid)
            state = (row.status, row.progress_stage)
            if state != previous:
                print(
                    json.dumps(
                        {
                            "generation_id": str(gid),
                            "status": row.status,
                            "stage": row.progress_stage,
                            "error_code": row.last_error_code,
                            "verification_status": (row.verification or {}).get(
                                "status"
                            ),
                            "has_result": bool(row.result_oss_key),
                            "quota_reserved": row.quota_reserved,
                        }
                    ),
                    flush=True,
                )
                previous = state
            if row.status not in {"processing", "cancel_requested"}:
                break
        await asyncio.sleep(5)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
