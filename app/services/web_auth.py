"""Password authentication and shared Redis throttling; no plaintext persistence."""

import asyncio
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from app.services.lock import get_redis

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    )
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


# Unknown accounts still perform a full password derivation.
DUMMY_HASH = f"pbkdf2_sha256${ITERATIONS}${'00' * 16}${'00' * 32}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != ITERATIONS:
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt_bytes, int(rounds)
        )
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


_LIMIT = """
local retry = 0
for i, key in ipairs(KEYS) do
  local count = redis.call('INCR', key)
  if count == 1 then redis.call('EXPIRE', key, ARGV[1]) end
  if count > tonumber(ARGV[i + 1]) then
    retry = math.max(retry, redis.call('TTL', key), 1)
  end
end
return retry
"""


async def throttle(request: Request, username: str, *, registration: bool = False):
    # Trust only ASGI's resolved peer. Never parse client-supplied forwarded headers.
    peer = request.client.host if request.client else "unknown"
    operation = "register" if registration else "login"
    identities = ["ip:" + peer, "account:" + username]
    keys = [
        f"auth:{operation}:" + hashlib.sha256(value.encode()).hexdigest()
        for value in identities
    ]
    window, ip_limit, account_limit = (3600, 10, 5) if registration else (900, 50, 10)
    try:
        async with asyncio.timeout(3):
            redis = await get_redis()
            retry = await redis.eval(
                _LIMIT, len(keys), *keys, window, ip_limit, account_limit
            )
    except Exception:
        raise HTTPException(503, "登录服务暂时不可用，请稍后重试") from None
    if retry:
        raise HTTPException(
            429, "尝试次数过多，请稍后重试", headers={"Retry-After": str(retry)}
        )
