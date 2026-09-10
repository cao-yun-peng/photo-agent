"""One atomic distributed budget for a plan, including API and worker calls."""

import asyncio
from dataclasses import dataclass
from contextvars import ContextVar
from contextlib import contextmanager
from time import monotonic
from uuid import UUID

from app.config import settings
from app.services.search_contracts import SearchError

_active: ContextVar["SearchBudget | None"] = ContextVar("search_budget", default=None)
_execution: ContextVar["SearchExecution | None"] = ContextVar(
    "search_execution", default=None
)
_COST = {"parse": 1, "embedding": 1, "text": 2, "visual": 10, "candidate": 0}


class BudgetExhausted(SearchError):
    def __init__(self, code="budget_exhausted"):
        super().__init__(code, 429)


class RequestTimeout(SearchError):
    """This execution stopped; the search plan can still be continued."""

    def __init__(self):
        super().__init__("request_timeout", 408)


@dataclass
class SearchExecution:
    deadline: float

    def remaining(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise RequestTimeout()
        return remaining


@contextmanager
def search_execution(timeout_seconds=None, *, fresh=False):
    """One local clock per request/job; nested search and fallback reuse it."""
    existing = _execution.get()
    if existing is not None and not fresh:
        yield existing
        return
    timeout = (
        settings.search_total_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    scope = SearchExecution(monotonic() + timeout)
    token = _execution.set(scope)
    try:
        yield scope
    finally:
        _execution.reset(token)


def execution_remaining() -> float:
    scope = _execution.get()
    return scope.remaining() if scope else settings.search_total_timeout_seconds


# Every reservation checks plan lifetime and quotas in the same Redis operation.
# Old hashes retain their old deadline; they are never migrated into fresh quotas.
_PLAN_REMAINING = """
if redis.call('exists', KEYS[1]) == 0 then return {-1,0} end
local t = redis.call('time')
local now = tonumber(t[1]) + tonumber(t[2])/1000000
local version = redis.call('hget', KEYS[1], 'schema_version')
local field = version == '2' and 'expires_at' or 'deadline'
local deadline = tonumber(redis.call('hget', KEYS[1], field))
if not deadline then return {-1,0} end
local remain = deadline-now
if remain <= 0 then
  if version == '2' then return {-1,0} else return {-2,0} end
end
"""


class SearchBudget:
    def __init__(self, redis, user_id, budget_id):
        self.redis = redis
        self.user_id = str(UUID(str(user_id)))
        self.key = f"search:v3:budget:{UUID(str(user_id))}:{UUID(str(budget_id))}"

    async def create(self) -> float:
        script = """
        local t = redis.call('time')
        local now = tonumber(t[1]) + tonumber(t[2])/1000000
        if redis.call('exists', KEYS[1]) == 1 then
          local expires = redis.call('hget', KEYS[1], 'expires_at')
          if expires then return expires end
          local ttl = redis.call('pttl', KEYS[1])
          if ttl <= 0 then return false end
          return string.format('%.6f', now+ttl/1000)
        end
        local expires = string.format('%.6f', now+tonumber(ARGV[5]))
        redis.call('hset', KEYS[1], 'schema_version', 2, 'expires_at', expires,
          'max_calls', ARGV[1], 'max_visual', ARGV[2], 'max_candidates', ARGV[3],
          'max_units', ARGV[4], 'calls', 0, 'visual', 0, 'candidate', 0, 'units', 0)
        redis.call('expire', KEYS[1], ARGV[5])
        return expires
        """
        expires = await self.redis.eval(
            script,
            1,
            self.key,
            settings.search_max_model_calls,
            settings.search_max_visual_calls,
            settings.search_max_verified_candidates,
            settings.search_max_budget_units,
            settings.search_snapshot_ttl_seconds,
        )
        if expires is None:
            raise BudgetExhausted()
        return float(expires)

    async def expires_at(self) -> float:
        expires = await self.redis.eval(
            """
            if redis.call('exists', KEYS[1]) == 0 then return false end
            local expires = redis.call('hget', KEYS[1], 'expires_at')
            if expires then return expires end
            local ttl = redis.call('pttl', KEYS[1])
            if ttl <= 0 then return false end
            local t = redis.call('time')
            return string.format('%.6f', tonumber(t[1])+tonumber(t[2])/1000000+ttl/1000)
            """,
            1,
            self.key,
        )
        if expires is None:
            raise BudgetExhausted()
        return float(expires)

    async def reserve(self, kind: str, amount=1) -> float:
        remaining = execution_remaining()
        if kind == "visual" and not settings.search_visual_verify_enabled:
            raise BudgetExhausted("visual_disabled")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
            raise ValueError("reservation amount must be a positive integer")
        script = (
            _PLAN_REMAINING
            + """
        local amount = tonumber(ARGV[2])
        local calls = ARGV[1] == 'candidate' and 0 or amount
        local visual = ARGV[1] == 'visual' and amount or 0
        local candidates = ARGV[1] == 'candidate' and amount or 0
        local units = tonumber(ARGV[3])*amount
        for _, pair in ipairs({{'calls',calls,'max_calls'}, {'visual',visual,'max_visual'},
                               {'candidate',candidates,'max_candidates'}, {'units',units,'max_units'}}) do
          if tonumber(redis.call('hget',KEYS[1],pair[1]) or 0)+pair[2] >
             tonumber(redis.call('hget',KEYS[1],pair[3])) then return {-3,0} end
        end
        redis.call('hincrby', KEYS[1], 'calls', calls)
        redis.call('hincrby', KEYS[1], 'visual', visual)
        redis.call('hincrby', KEYS[1], 'candidate', candidates)
        redis.call('hincrby', KEYS[1], 'units', units)
        redis.call('hincrby', KEYS[1], 'stage:'..ARGV[1], amount)
        return {1, math.floor(remain*1000)}
        """
        )
        result = await self.redis.eval(script, 1, self.key, kind, amount, _COST[kind])
        if result[0] != 1:
            raise BudgetExhausted(
                "deadline_exceeded" if result[0] == -2 else "budget_exhausted"
            )
        return min(remaining, execution_remaining(), result[1] / 1000)

    async def remaining(self) -> float:
        result = await self.redis.eval(
            _PLAN_REMAINING + "return {1, math.floor(remain*1000)}", 1, self.key
        )
        if result[0] != 1:
            raise BudgetExhausted(
                "deadline_exceeded" if result[0] == -2 else "budget_exhausted"
            )
        return min(execution_remaining(), result[1] / 1000)

    async def usage(self):
        values = await self.redis.hgetall(self.key)
        return {
            k: float(v)
            for k, v in values.items()
            if k not in {"deadline", "expires_at", "schema_version"}
        }

    @contextmanager
    def activate(self):
        with search_execution():
            token = _active.set(self)
            try:
                yield
            finally:
                _active.reset(token)


async def model_call(kind, operation):
    """Reserve immediately before an actual cache-miss call; never refund unknown outcomes."""
    budget = _active.get()
    if budget is None:
        if kind == "visual" and not settings.search_visual_verify_enabled:
            raise BudgetExhausted("visual_disabled")
        return await operation()
    remaining = await budget.reserve(kind)
    timeout = asyncio.timeout(remaining)
    try:
        async with timeout:
            return await operation()
    except TimeoutError as exc:
        if not timeout.expired():
            raise
        # Preserve legacy deadline/plan expiry errors; otherwise only this request
        # timed out. Reservations survive cancellation and unknown provider results.
        await budget.remaining()
        raise RequestTimeout() from exc


async def record_provider_usage(usage):
    """Provider token counts are measurements, not inferred currency costs."""
    budget = _active.get()
    if budget is None or not isinstance(usage, dict):
        return
    for target, names in {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }.items():
        value = next(
            (usage[n] for n in names if isinstance(usage.get(n), (int, float))), None
        )
        if value is not None and value >= 0:
            await budget.redis.eval(
                "if redis.call('exists',KEYS[1]) == 1 then return redis.call('hincrby',KEYS[1],ARGV[1],ARGV[2]) end return 0",
                1,
                budget.key,
                target,
                int(value),
            )
