"""Versioned distributed singleflight: followers wait without starting paid work."""

import asyncio
import hashlib
import json
import time
from uuid import uuid4
from app.config import settings
from app.services.lock import get_redis
from app.services.search_budget import (
    _active,
    BudgetExhausted,
    RequestTimeout,
    execution_remaining,
    search_execution,
)
from app.services.search_contracts import SearchError
from app.services.task_lifecycle import run_cleanup

CACHE_VERSION = "search-cache-v4"


def cache_key(stage, payload):
    budget = _active.get()
    scope = budget.user_id if budget else "standalone"
    raw = json.dumps(
        [CACHE_VERSION, settings.search_cache_revision, scope, stage, payload],
        sort_keys=True,
        default=str,
    )
    return "search:v4:cache:" + hashlib.sha256(raw.encode()).hexdigest()


async def _stat(field):
    budget = _active.get()
    if budget:
        await budget.redis.eval(
            "if redis.call('exists',KEYS[1]) == 1 then return redis.call('hincrby',KEYS[1],ARGV[1],1) end return 0",
            1,
            budget.key,
            field,
        )


async def cached_call(key, compute, *, ttl=3600, validate=lambda value: value):
    with search_execution():
        return await _cached_call(key, compute, ttl=ttl, validate=validate)


async def _cached_call(key, compute, *, ttl, validate):
    redis = await get_redis()
    budget = _active.get()
    remaining = await budget.remaining() if budget else execution_remaining()
    deadline = time.monotonic() + remaining
    waited = False
    while True:
        if await redis.exists(key + ":failed"):
            raise SearchError("search_dependency_failed", 503)
        raw = await redis.get(key)
        if raw is not None:
            try:
                value = validate(json.loads(raw))
            except (ValueError, TypeError, KeyError):
                # Don't delete a replacement written since this read.
                await redis.eval(
                    "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) end return 0",
                    1,
                    key,
                    raw,
                )
            else:
                await _stat("cache_hits")
                return value, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if budget:
                await budget.remaining()
            raise RequestTimeout()
        token = str(uuid4())
        lock_key = key + ":flight"
        if await redis.set(lock_key, token, nx=True, px=int((remaining + 5) * 1000)):
            timeout = None
            try:
                # Close read/acquire race; a previous owner may have just published.
                raw = await redis.get(key)
                if raw is not None:
                    try:
                        value = validate(json.loads(raw))
                    except (ValueError, TypeError, KeyError):
                        pass
                    else:
                        await _stat("cache_hits")
                        return value, True
                if await redis.exists(key + ":failed"):
                    raise SearchError("search_dependency_failed", 503)
                await _stat("cache_misses")
                timeout = asyncio.timeout(remaining)
                async with timeout:
                    value = await compute()
                value = validate(value)
                accepted = await redis.eval(
                    "if redis.call('get',KEYS[2]) ~= ARGV[1] then return 0 end redis.call('set',KEYS[1],ARGV[2],'EX',ARGV[3]) return 1",
                    2,
                    key,
                    lock_key,
                    token,
                    json.dumps(value),
                    ttl,
                )
                if not accepted:
                    raise SearchError("cache_ownership_lost")
                return value, False
            except TimeoutError as exc:
                if timeout is None or not timeout.expired():
                    raise
                if budget:
                    await budget.remaining()
                raise RequestTimeout() from exc
            except Exception as exc:
                if not isinstance(exc, (BudgetExhausted, SearchError)):
                    await redis.eval(
                        "if redis.call('get',KEYS[2]) == ARGV[1] then redis.call('set',KEYS[1],'upstream_failed','EX',2) end return 0",
                        2,
                        key + ":failed",
                        lock_key,
                        token,
                    )
                raise
            finally:

                async def release():
                    await redis.eval(
                        "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) end return 0",
                        1,
                        lock_key,
                        token,
                    )

                await run_cleanup(release)
        if not waited:
            await _stat("coalesced_waits")
            waited = True
        await asyncio.sleep(min(0.02, remaining))
