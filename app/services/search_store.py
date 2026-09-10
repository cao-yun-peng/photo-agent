"""Signed user-bound cursors and token-checked snapshot mutation."""

import base64
import hashlib
import hmac
import json
import time
import math
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
from datetime import datetime
from app.services.task_lifecycle import run_cleanup
from app.config import settings
from app.services.search_contracts import SearchError, QueryPlan


def _key(user, plan):
    return f"search:v3:plan:{UUID(str(user))}:{UUID(str(plan))}"


def encode_cursor(user, plan_id, offset, expires):
    payload = json.dumps(
        [
            4 if isinstance(offset, dict) else 3,
            str(user),
            str(plan_id),
            offset,
            expires,
        ],
        separators=(",", ":"),
    )
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(
        settings.jwt_secret.encode(),
        ("search-cursor-v3:" + raw).encode(),
        hashlib.sha256,
    ).hexdigest()
    return raw + "." + signature


def decode_cursor(value, user):
    try:
        raw, sig = value.split(".")
        expected = hmac.new(
            settings.jwt_secret.encode(),
            ("search-cursor-v3:" + raw).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        version, owner, plan, offset, expires = json.loads(
            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        )
        if owner != str(user):
            raise ValueError
        if version == 3:
            if not isinstance(offset, int) or offset < 0:
                raise ValueError
        elif version == 4:
            if not isinstance(offset, dict) or set(offset) not in (
                {"id", "taken_at"},
                {"id", "score"},
            ):
                raise ValueError
            UUID(offset["id"])
            if "score" in offset:
                if not isinstance(offset["score"], (int, float)) or not math.isfinite(
                    offset["score"]
                ):
                    raise ValueError
            elif offset["taken_at"] is not None:
                if datetime.fromisoformat(offset["taken_at"]).tzinfo is None:
                    raise ValueError
        else:
            raise ValueError
        UUID(plan)
        if expires <= time.time():
            raise SearchError("cursor_expired", 410)
        return plan, offset
    except SearchError:
        raise
    except (ValueError, TypeError, KeyError):
        raise SearchError("invalid_cursor", 400) from None


class SearchStore:
    def __init__(self, redis):
        self.redis = redis

    async def load(self, user, plan_id):
        raw = await self.redis.get(_key(user, plan_id))
        if not raw:
            raise SearchError("search_expired", 410)
        value = json.loads(raw)
        plan = QueryPlan.model_validate(value["plan"])
        if str(plan.user_id) != str(user) or plan.expires_at <= time.time():
            raise SearchError("search_expired", 410)
        return value

    async def create(self, snapshot):
        plan = QueryPlan.model_validate(snapshot["plan"])
        if plan.expires_at <= time.time():
            raise SearchError("search_expired", 410)
        await self.redis.set(
            _key(plan.user_id, plan.id),
            json.dumps(snapshot),
            pxat=int(plan.expires_at * 1000),
            nx=True,
        )

    @asynccontextmanager
    async def mutation(self, user, plan_id):
        key = _key(user, plan_id)
        token = str(uuid4())
        lease = int(
            settings.search_total_timeout_seconds
            + settings.task_cleanup_timeout_seconds
            + 30
        )
        if not await self.redis.set(key + ":lock", token, ex=lease, nx=True):
            raise SearchError("search_busy", 409)
        try:
            snapshot = await self.load(user, plan_id)

            async def persist():
                script = """
                if redis.call('get', KEYS[2]) ~= ARGV[1] then return 0 end
                local ttl = redis.call('pttl', KEYS[1])
                if ttl <= 0 then return 0 end
                redis.call('set', KEYS[1], ARGV[2], 'PX', ttl)
                return 1
                """
                if not await self.redis.eval(
                    script, 2, key, key + ":lock", token, json.dumps(snapshot)
                ):
                    raise SearchError("search_ownership_lost", 409)

            try:
                yield snapshot
            except BaseException:
                await run_cleanup(persist)
                raise
            else:
                await persist()
        finally:

            async def release():
                await self.redis.eval(
                    "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) end return 0",
                    1,
                    key + ":lock",
                    token,
                )

            await run_cleanup(release)
