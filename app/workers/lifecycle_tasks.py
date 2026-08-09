"""数据生命周期管理任务。

user_events 是写入最频繁的表。本模块提供：
- 按月分区创建（由 Alembic 迁移负责建表结构，这里只做辅助检查）
- 冷数据归档：> 180 天的事件导出为 NDJSON 上传到 OSS 后从 PG 删除
- 画像聚合的定时触发

注意：user_events 表使用 PostgreSQL 原生分区（RANGE created_at），
由迁移脚本建立。查询时 PG 会自动裁剪分区。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from app.database import AsyncSessionLocal
from app.models.user_event import UserEvent
from app.services.oss import put_object

logger = logging.getLogger(__name__)

# 数据生命周期阈值
_HOT_DAYS = 30    # 热数据：最近 30 天
_WARM_DAYS = 180  # 温数据：30-180 天
_COLD_DAYS = 180  # 冷数据：超过 180 天归档


async def archive_cold_events(
    ctx: dict[str, Any],
    batch_size: int = 5000,
) -> dict[str, Any]:
    """ARQ 任务：将超过 _COLD_DAYS 天的 user_events 归档到 OSS 后删除。

    流程：
      1. 按 created_at 排序选取一批老事件；
      2. 写入 NDJSON（每行一条 JSON）；
      3. 上传到自己 OSS；
      4. 删除 PG 中的对应记录。

    返回 {"archived": int, "oss_key": str | None}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=_COLD_DAYS)
    archived = 0
    oss_key: str | None = None

    async with AsyncSessionLocal() as db:
        stmt = (
            select(UserEvent)
            .where(UserEvent.created_at < cutoff)
            .order_by(UserEvent.created_at)
            .limit(batch_size)
        )
        result = await db.execute(stmt)
        events = result.scalars().all()

        if not events:
            return {"ok": True, "archived": 0, "oss_key": None}

        lines = []
        event_ids = []
        for ev in events:
            event_ids.append(ev.id)
            lines.append(
                json.dumps(
                    {
                        "id": ev.id,
                        "user_id": str(ev.user_id),
                        "event_type": ev.event_type,
                        "payload": ev.payload,
                        "created_at": ev.created_at.isoformat() if ev.created_at else None,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )

        content = "\n".join(lines).encode("utf-8")
        today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        oss_key = f"archives/user_events/{today}/{len(events)}_{int(datetime.now(timezone.utc).timestamp())}.ndjson"

        try:
            await put_object(oss_key, content, content_type="application/x-ndjson")
        except Exception as exc:  # noqa: BLE001
            logger.exception("archive upload failed | key=%s", oss_key)
            return {"ok": False, "archived": 0, "oss_key": None, "error": str(exc)}

        # 上传成功后删除原记录
        await db.execute(
            delete(UserEvent).where(UserEvent.id.in_(event_ids))
        )
        await db.commit()
        archived = len(events)
        logger.info(
            "archive_cold_events done | archived=%s oss_key=%s",
            archived,
            oss_key,
        )

    return {"ok": True, "archived": archived, "oss_key": oss_key}


async def count_events_by_age(
    ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ARQ 任务 / 管理命令：统计热/温/冷数据分布。"""
    from sqlalchemy import func

    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(days=_HOT_DAYS)
    warm_cutoff = now - timedelta(days=_WARM_DAYS)

    async with AsyncSessionLocal() as db:
        hot_count = (
            await db.execute(
                select(func.count(UserEvent.id)).where(
                    UserEvent.created_at >= hot_cutoff
                )
            )
        ).scalar() or 0
        warm_count = (
            await db.execute(
                select(func.count(UserEvent.id)).where(
                    UserEvent.created_at >= warm_cutoff,
                    UserEvent.created_at < hot_cutoff,
                )
            )
        ).scalar() or 0
        cold_count = (
            await db.execute(
                select(func.count(UserEvent.id)).where(
                    UserEvent.created_at < warm_cutoff
                )
            )
        ).scalar() or 0

    return {
        "ok": True,
        "hot": hot_count,
        "warm": warm_count,
        "cold": cold_count,
        "thresholds": {
            "hot_days": _HOT_DAYS,
            "warm_days": _WARM_DAYS,
            "cold_days": _COLD_DAYS,
        },
    }
