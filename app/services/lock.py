"""Redis 分布式锁：用于 Agent 会话并发控制。

用 Redis SET NX EX 实现原子加锁 + 自动过期，避免进程崩溃导致的死锁。
锁 key 格式：lock:agent:{user_id}
锁 value：随机 token，释放/续期时校验 token 防止误操作别人的锁。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.services.task_lifecycle import OwnershipLost
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# 全局 Redis 客户端；延迟初始化
_redis_client: aioredis.Redis | None = None

# Lua 脚本：原子 release —— 仅当 key 的值 == token 时才 del
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua 脚本：原子 extend —— 仅当 key 的值 == token 时才 expire
_EXTEND_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class SessionBusyError(Exception):
    """用户已有活跃的 Agent 会话。"""

    def __init__(self, retry_after: int = 5):
        self.retry_after = retry_after
        super().__init__(f"Agent is busy, retry after {retry_after}s")


async def get_redis() -> aioredis.Redis:
    """获取或初始化全局 Redis 异步客户端。"""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _redis_client


class AgentLock:
    """基于 Redis 的用户级 Agent 锁（token 校验 + 自动续期）。

    使用 try/finally 确保释放：
        lock = AgentLock(user_id)
        if not await lock.acquire():
            raise SessionBusyError()
        renew_task = await lock.start_auto_renew()
        try:
            ...  # 运行 Agent
        finally:
            renew_task.cancel()
            await lock.release()
    """

    def __init__(self, user_id: str, redis: aioredis.Redis | None = None) -> None:
        self.user_id = user_id
        self.key = f"lock:agent:{user_id}"
        self.redis = redis
        self._token: str | None = None
        self.lost = asyncio.Event()
        self._valid_until = 0.0

    async def _get_redis(self) -> aioredis.Redis:
        if self.redis is None:
            self.redis = await get_redis()
        return self.redis

    async def acquire(self, ttl: int | None = None) -> bool:
        """尝试获取锁。返回 True = 拿到，False = 被占用。

        锁 value 为随机 token，release/extend 时校验，防止误操作。
        """
        ttl = ttl or settings.agent_lock_ttl
        redis = await self._get_redis()
        self._token = uuid4().hex
        started = time.monotonic()
        result = await asyncio.wait_for(
            redis.set(self.key, self._token, nx=True, ex=ttl), timeout=max(1, ttl / 3)
        )
        self._valid_until = started + ttl
        self.lost.clear()
        acquired = result is not None
        if acquired:
            logger.debug(
                "agent lock acquired | user=%s token=%s ttl=%d",
                self.user_id,
                self._token,
                ttl,
            )
        else:
            self._token = None
            logger.debug("agent lock busy | user=%s", self.user_id)
        return acquired

    async def release(self) -> bool:
        """释放锁。仅当 token 匹配时才删除，防止删除别人的锁。

        返回 True = 成功释放，False = token 不匹配（锁已过期或被他人获取）。
        """
        if self._token is None:
            return False
        redis = await self._get_redis()
        result = await redis.eval(_RELEASE_SCRIPT, 1, self.key, self._token)
        released = bool(result)
        if released:
            logger.debug("agent lock released | user=%s", self.user_id)
        else:
            logger.warning(
                "agent lock release skipped (token mismatch or expired) | user=%s",
                self.user_id,
            )
        self._token = None
        return released

    async def extend(self, ttl: int | None = None) -> bool:
        """续期：Agent 多轮执行可能超过初始 TTL，每次 Tool 返回后续期。

        仅当 token 匹配时才续期，防止给别人的锁续期。
        """
        if self._token is None:
            return False
        ttl = ttl or settings.agent_lock_ttl
        redis = await self._get_redis()
        result = await redis.eval(_EXTEND_SCRIPT, 1, self.key, self._token, str(ttl))
        return bool(result)

    async def assert_owned(self) -> None:
        if (
            self.lost.is_set()
            or self._token is None
            or time.monotonic() >= self._valid_until
        ):
            self.lost.set()
            raise OwnershipLost("agent_lock_lost")
        try:
            redis = await self._get_redis()
            value = await asyncio.wait_for(
                redis.get(self.key), timeout=max(1, settings.agent_lock_ttl / 3)
            )
            if value != self._token or time.monotonic() >= self._valid_until:
                raise OwnershipLost("agent_lock_lost")
        except Exception as exc:
            self.lost.set()
            raise OwnershipLost("agent_lock_lost") from exc

    async def start_auto_renew(self, interval: float | None = None) -> asyncio.Task:
        interval = (
            interval if interval is not None else max(settings.agent_lock_ttl / 3, 0.1)
        )

        async def renew_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    started = time.monotonic()
                    if started >= self._valid_until or not await asyncio.wait_for(
                        self.extend(), timeout=max(1, settings.agent_lock_ttl / 3)
                    ):
                        self.lost.set()
                        return
                    self._valid_until = started + settings.agent_lock_ttl
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("agent lock renewal failed | user=%s", self.user_id)
                self.lost.set()

        return asyncio.create_task(renew_loop(), name="agent-lock-renew")

    async def is_locked(self) -> bool:
        """检查当前是否被锁定。"""
        redis = await self._get_redis()
        return bool(await redis.exists(self.key))

    def to_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "key": self.key}
