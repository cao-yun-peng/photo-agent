"""Durable, bounded photo attempts. PostgreSQL is the recovery source of truth."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update

from app.config import settings
from app.core.telemetry import enqueue_job_with_trace
from app.database import AsyncSessionLocal
from app.models.photo import Photo
from app.services.task_lifecycle import OwnershipLost

logger = logging.getLogger(__name__)


async def claim_photo(photo_id: str):
    token = uuid4()
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                update(Photo)
                .where(
                    Photo.id == UUID(photo_id),
                    Photo.processing_attempts < settings.photo_processing_max_attempts,
                    or_(
                        and_(
                            Photo.status == "pending",
                            or_(
                                Photo.processing_retry_at.is_(None),
                                Photo.processing_retry_at <= func.now(),
                            ),
                        ),
                        and_(
                            Photo.status == "processing",
                            or_(
                                Photo.processing_lease_until.is_(None),
                                Photo.processing_lease_until <= func.now(),
                            ),
                        ),
                    ),
                )
                .values(
                    status="processing",
                    processing_token=token,
                    processing_started_at=func.now(),
                    processing_lease_until=func.now()
                    + timedelta(seconds=settings.photo_processing_lease_seconds),
                    processing_attempts=Photo.processing_attempts + 1,
                    processing_retry_at=None,
                    processing_dispatch_until=None,
                )
                .returning(Photo.processing_attempts)
            )
        ).scalar_one_or_none()
        await db.commit()
    return (token, row) if row is not None else None


def photo_commit_guard(photo_id: str, token, lost: asyncio.Event):
    async def guard(db):
        current = (
            await db.execute(
                select(
                    Photo.processing_token, Photo.processing_lease_until > func.now()
                )
                .where(Photo.id == UUID(photo_id))
                .with_for_update()
            )
        ).one_or_none()
        if lost.is_set() or current is None or current[0] != token or not current[1]:
            lost.set()
            raise OwnershipLost("photo_lease_lost")

    return guard


async def renew_photo(photo_id: str, token) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Photo)
            .where(
                Photo.id == UUID(photo_id),
                Photo.processing_token == token,
                Photo.processing_lease_until > func.now(),
            )
            .values(
                processing_lease_until=func.now()
                + timedelta(seconds=settings.photo_processing_lease_seconds)
            )
        )
        await db.commit()
        return result.rowcount == 1


async def release_attempt(
    photo_id: str, token, *, retry: bool, reason: str, delay: int = 5
) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Photo)
            .where(
                Photo.id == UUID(photo_id),
                Photo.processing_token == token,
                Photo.status == "processing",
            )
            .values(
                status="pending" if retry else "failed",
                partial_reason=reason[:32],
                processing_token=None,
                processing_lease_until=None,
                processing_dispatch_until=None,
                processing_retry_at=func.now() + timedelta(seconds=delay)
                if retry
                else None,
            )
        )
        await db.commit()
        return result.rowcount == 1


async def recover_photo_jobs(ctx) -> dict:
    """Bounded outbox-like scan also repairs DB-commit/enqueue-failure windows."""
    now = datetime.now(timezone.utc)
    eligible = or_(
        and_(
            Photo.status == "processing",
            or_(
                Photo.processing_lease_until.is_(None),
                Photo.processing_lease_until <= func.now(),
            ),
        ),
        and_(
            Photo.status == "pending",
            or_(
                Photo.processing_retry_at.is_(None),
                Photo.processing_retry_at <= func.now(),
            ),
        ),
    )
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Photo.id, Photo.processing_attempts)
                .where(
                    eligible,
                    or_(
                        Photo.processing_dispatch_until.is_(None),
                        Photo.processing_dispatch_until <= func.now(),
                    ),
                )
                .order_by(Photo.created_at, Photo.id)
                .limit(settings.photo_recovery_batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        jobs = []
        for photo_id, attempts in rows:
            exhausted = attempts >= settings.photo_processing_max_attempts
            await db.execute(
                update(Photo)
                .where(Photo.id == photo_id)
                .values(
                    status="failed" if exhausted else "pending",
                    processing_token=None,
                    processing_lease_until=None,
                    processing_dispatch_until=None
                    if exhausted
                    else now + timedelta(seconds=30),
                    partial_reason="process_retry_exhausted"
                    if exhausted
                    else "process_recovering",
                    # Durable redispatch after a queue failure; avoids hot-loop scanning.
                    processing_retry_at=None if exhausted else now,
                )
            )
            if not exhausted:
                jobs.append((str(photo_id), attempts))
        await db.commit()
    queued = 0
    for photo_id, attempts in jobs:
        try:
            await enqueue_job_with_trace(
                ctx["redis"],
                "process_photo",
                photo_id,
                _job_id=f"photo-recovery:{photo_id}:{attempts}:{int(now.timestamp())}",
            )
            queued += 1
        except Exception:
            logger.exception("photo recovery enqueue failed | photo=%s", photo_id)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Photo)
                    .where(
                        Photo.id == UUID(photo_id),
                        Photo.status == "pending",
                        Photo.processing_dispatch_until == now + timedelta(seconds=30),
                    )
                    .values(processing_dispatch_until=None)
                )
                await db.commit()
    return {"scanned": len(rows), "queued": queued}
