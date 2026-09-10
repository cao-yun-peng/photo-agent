"""Generation-scoped candidate pools; every Redis operation checks the current scope."""

import asyncio
import json
import time
from uuid import UUID, uuid4

from app.config import settings
from app.services.lock import get_redis


def _scope_parts(scope):
    parts = str(scope).split(":")
    if len(parts) != 3:
        raise ValueError("Unversioned candidate pool")
    return tuple(str(UUID(part)) for part in parts)


def candidate_pool_key(scope):
    _scope_parts(scope)
    return f"agent:search-pool:v2:{scope}"


def candidate_status_key(scope):
    return candidate_pool_key(scope) + ":status"


def candidate_trace_key(scope):
    return candidate_pool_key(scope) + ":trace"


def _current_key(scope):
    user, session, _ = _scope_parts(scope)
    return f"agent:search-current:v2:{user}:{session}"


async def begin_candidate_search(user_id, session_id, lock_token=None):
    scope = f"{user_id}:{session_id}:{uuid4()}"
    redis = await get_redis()
    script = """
    if ARGV[3] ~= '' and redis.call('get', KEYS[2]) ~= ARGV[3] then return 0 end
    redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
    return 1
    """
    accepted = await redis.eval(
        script,
        2,
        _current_key(scope),
        f"lock:agent:{user_id}",
        scope,
        settings.agent_search_pool_ttl_seconds,
        lock_token or "",
    )
    if not accepted:
        raise RuntimeError("search_owner_lost")
    return scope


# Lua makes generation check and mutation indivisible.
_SCRIPT = """
if redis.call('get', KEYS[1]) ~= ARGV[1] then return nil end
local op = ARGV[3]
if op == 'get' then return redis.call('get', KEYS[2]) end
if op == 'size' then return redis.call('llen', KEYS[2]) end
if op == 'pop' then return redis.call('lpop', KEYS[2]) end
if op == 'claim' then
    if redis.call('exists', KEYS[2]) == 1 then return 0 end
    redis.call('set', KEYS[2], 'queued', 'EX', ARGV[2])
    return 1
end
if op == 'set' then
    redis.call('set', KEYS[2], ARGV[4], 'EX', ARGV[2])
    return 1
end
if op == 'push' then
    for i = 4, #ARGV do redis.call('rpush', KEYS[2], ARGV[i]) end
    redis.call('expire', KEYS[2], ARGV[2])
    return #ARGV - 3
end
if op == 'clear' then return redis.call('del', KEYS[2], KEYS[3], KEYS[4]) end
"""


async def _operation(scope, key, op, *args):
    redis = await get_redis()
    return await redis.eval(
        _SCRIPT,
        4,
        _current_key(scope),
        key,
        candidate_status_key(scope),
        candidate_trace_key(scope),
        str(scope),
        settings.agent_search_pool_ttl_seconds,
        op,
        *args,
    )


async def claim_prefetch(scope):
    return await _operation(scope, candidate_status_key(scope), "claim")


async def clear_candidate_pool(scope):
    return await _operation(scope, candidate_pool_key(scope), "clear")


async def set_prefetch_status(scope, status):
    return bool(await _operation(scope, candidate_status_key(scope), "set", status))


async def get_prefetch_status(scope):
    try:
        return str(
            await _operation(scope, candidate_status_key(scope), "get") or "missing"
        )
    except ValueError:
        return "missing"  # Old persisted sessions cannot access unversioned pools.


async def set_candidate_trace_context(scope, carrier):
    return bool(
        await _operation(scope, candidate_trace_key(scope), "set", json.dumps(carrier))
    )


async def get_candidate_trace_context(scope):
    raw = await _operation(scope, candidate_trace_key(scope), "get")
    try:
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


async def push_verified_candidates(scope, items):
    if not items:
        return 0
    # Never retain expiring object capabilities in a cached candidate.
    items = [
        {k: v for k, v in item.items() if not k.endswith("_url")} for item in items
    ]
    return int(
        await _operation(
            scope,
            candidate_pool_key(scope),
            "push",
            *(json.dumps(item, ensure_ascii=False) for item in items),
        )
        or 0
    )


async def pop_verified_candidate(scope):
    for _ in range(100):
        raw = await _operation(scope, candidate_pool_key(scope), "pop")
        if raw is None:
            return None
        try:
            item = json.loads(raw)
            if isinstance(item, dict) and item.get("id"):
                return item
        except (ValueError, TypeError):
            continue
    return None


async def candidate_pool_size(scope):
    return int(await _operation(scope, candidate_pool_key(scope), "size") or 0)


async def wait_for_verified_candidate(scope, *, timeout_seconds=None):
    timeout = (
        settings.agent_search_prefetch_wait_seconds
        if timeout_seconds is None
        else max(0, timeout_seconds)
    )
    deadline = time.monotonic() + timeout
    while True:
        item = await pop_verified_candidate(scope)
        if item is not None:
            return item
        if (
            await get_prefetch_status(scope) not in {"queued", "running"}
            or time.monotonic() >= deadline
        ):
            return None
        await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
