"""Agent v2 稳定灰度分桶。"""

from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings


def rollout_bucket(user_id: Any, *, salt: str | None = None) -> int:
    raw = f"{salt or settings.agent_v2_rollout_salt}:{user_id}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:8], 16) % 100


def agent_variant_for_user(user_id: Any) -> str:
    if settings.agent_v2_kill_switch or not settings.agent_v2_enabled:
        return "control"
    percent = max(0, min(100, int(settings.agent_v2_rollout_percent)))
    return "v2" if rollout_bucket(user_id) < percent else "control"
