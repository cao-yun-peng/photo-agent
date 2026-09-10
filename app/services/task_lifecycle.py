"""Task cleanup and database write fencing, shared by API and workers."""

import asyncio
import logging

import anyio

from app.config import settings

logger = logging.getLogger(__name__)


class OwnershipLost(RuntimeError):
    """The current attempt must not commit or start more work."""


_draining_tasks: set[asyncio.Task] = set()


def _finish(task: asyncio.Task) -> None:
    _draining_tasks.discard(task)
    if not task.cancelled():
        task.exception()  # retrieve failures even if the caller already disconnected


async def cancel_and_wait(*tasks: asyncio.Task | None) -> None:
    active = [task for task in tasks if task is not None]
    if not active:
        return
    for task in active:
        if not task.done():
            task.cancel()
    # A wait_for(gather(...)) can exceed its deadline if a child ignores cancel.
    with anyio.CancelScope(shield=True):
        done, pending = await asyncio.wait(
            active, timeout=settings.task_cleanup_timeout_seconds
        )
        for task in done:
            _finish(task)
        if pending:
            logger.error("task cleanup exceeded deadline | pending=%d", len(pending))
            for task in pending:
                _draining_tasks.add(task)
                task.add_done_callback(_finish)


async def run_cleanup(operation) -> None:
    with anyio.CancelScope(shield=True):
        task = asyncio.create_task(operation())
        done, _ = await asyncio.wait(
            {task}, timeout=settings.task_cleanup_timeout_seconds
        )
        if done:
            try:
                task.result()
            except Exception:
                logger.exception("task resource cleanup failed")
        else:
            task.cancel()
            _draining_tasks.add(task)
            task.add_done_callback(_finish)
            logger.error("resource cleanup exceeded deadline")
