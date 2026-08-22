"""存量照片结构化分析迁移命令行入口。

用法：
    # 默认只查看待重索引数量，不调用模型
    python scripts/migrate_structured_analysis.py

    # 确认后执行
    python scripts/migrate_structured_analysis.py --apply --batch-size 50

    # 限速持续迁移，每次之间 sleep N 秒
    python scripts/migrate_structured_analysis.py --batch-size 20 --interval 12 --max-batches 100

    # 只迁移某个用户
    python scripts/migrate_structured_analysis.py --user-id <uuid>

参数：
    --batch-size   每批处理照片数，默认 50
    --interval     每批之间间隔秒数，默认 12（约 5 req/min）
    --max-batches  最大批次数，默认不限制
    --user-id      只迁移指定用户
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.workers.migrate_tasks import (  # noqa: E402
    count_pending_semantic_reindex,
    migrate_photos_batch,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    parser = argparse.ArgumentParser(description="存量照片结构化分析迁移")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际执行 VL + embedding 重索引；省略时仅预览数量",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="每批处理照片数",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=12,
        help="每批之间间隔秒数（默认 12，约 5 req/min）",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="最大批次数，默认不限制",
    )
    parser.add_argument(
        "--user-id",
        type=str,
        default=None,
        help="只迁移指定用户",
    )
    args = parser.parse_args()

    if not args.apply:
        pending = await count_pending_semantic_reindex(args.user_id)
        logger.info(
            "待执行 v5 语义重索引：%s 张。确认模型调用成本后加 --apply 执行。",
            pending,
        )
        return 0

    total_processed = 0
    total_upgraded = 0
    total_failed = 0
    batches = 0

    while True:
        if args.max_batches is not None and batches >= args.max_batches:
            logger.info("达到最大批次数 %s，停止迁移", args.max_batches)
            break

        result = await migrate_photos_batch(
            {},
            batch_size=args.batch_size,
            only_user_id=args.user_id,
        )
        batches += 1
        total_processed += result["processed"]
        total_upgraded += result["upgraded"]
        total_failed += result["failed"]

        logger.info(
            "批次 %s | processed=%s upgraded=%s failed=%s",
            batches,
            result["processed"],
            result["upgraded"],
            result["failed"],
        )

        if result["processed"] == 0:
            logger.info("没有更多需要迁移的照片，结束")
            break

        if args.interval > 0:
            time.sleep(args.interval)

    logger.info(
        "迁移结束 | 总批次数=%s 处理=%s 成功=%s 失败=%s",
        batches,
        total_processed,
        total_upgraded,
        total_failed,
    )
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
