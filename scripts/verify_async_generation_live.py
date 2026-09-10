"""User-authorized single live prepare/confirm/worker run, with a stable request key."""

import asyncio
import json
import logging
from time import perf_counter
from uuid import UUID

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models.generation import Generation
from app.schemas.creative_plan import PackageOptions
from app.services.generation_service import prepare_generation, confirm_generation


async def main():
    engine.echo = False
    logging.disable(logging.CRITICAL)
    assert settings.openai_image_transport == "timicc_async"
    async with AsyncSessionLocal() as db:
        source = await db.get(Generation, UUID("4422cca8-53ac-41dd-a60b-124ffce3506b"))
        gen = await prepare_generation(
            db=db,
            user_id=source.user_id,
            photo_id=source.source_photo_id,
            skill_id=source.skill_id,
            extra_prompt=source.extra_prompt,
            model="gpt-image-2",
            package_options=PackageOptions.model_validate(
                source.execution_snapshot["title_options"]
            ),
            idempotency_key="async-fix-live-20260910-01",
        )
        gid = gen.id
        print(json.dumps({"generation_id": str(gid), "status": gen.status}), flush=True)
        if gen.status == "awaiting_confirmation":
            await confirm_generation(
                db=db,
                user_id=source.user_id,
                generation_id=gid,
                confirmation_token=gen.confirmation_token,
                execution_digest=gen.execution_digest,
            )
    previous = None
    started = perf_counter()
    while perf_counter() - started < 900:
        async with AsyncSessionLocal() as db:
            row = await db.get(Generation, gid)
            state = (row.status, row.progress_stage, row.last_error_code)
            if state != previous:
                print(
                    json.dumps(
                        {
                            "generation_id": str(gid),
                            "status": row.status,
                            "stage": row.progress_stage,
                            "error_code": row.last_error_code,
                            "elapsed_s": round(perf_counter() - started),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                previous = state
            if row.status not in {"pending", "processing", "cancel_requested"}:
                print(
                    json.dumps(
                        {
                            "generation_id": str(gid),
                            "status": row.status,
                            "attempt_count": row.attempt_count,
                            "quota_reserved": row.quota_reserved,
                            "has_result": bool(row.result_oss_key),
                            "estimated_yuan": str(row.cost_yuan),
                            "verification_status": (row.verification or {}).get(
                                "status"
                            ),
                            "dimensions": (row.verification or {}).get("dimensions"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                break
        await asyncio.sleep(5)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
