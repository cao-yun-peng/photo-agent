"""Fence every Agent transaction against the current user's run token."""

from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.models.user import User
from app.services.task_lifecycle import OwnershipLost


async def claim_agent_run(db, user_id: UUID, lock):
    await lock.assert_owned()
    token = uuid4()
    result = await db.execute(
        update(User).where(User.id == user_id).values(agent_run_token=token)
    )
    if result.rowcount != 1:
        raise OwnershipLost("user_missing")
    await db.commit()
    await lock.assert_owned()

    async def guard(session):
        await lock.assert_owned()
        # Locked through commit: a takeover and a commit are serialized in PG.
        current = await session.scalar(
            select(User.agent_run_token).where(User.id == user_id).with_for_update()
        )
        if current != token or lock.lost.is_set():
            lock.lost.set()
            raise OwnershipLost("agent_run_replaced")

    db.info["agent_lock_token"] = lock._token
    db.info["commit_guard"] = guard
    db.info["ownership_check"] = lock.assert_owned
    return token
