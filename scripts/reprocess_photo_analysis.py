"""安全重算隔离评测用户的 VL v4 分析和 embedding。

默认只打印计划；必须显式传 ``--apply`` 才调用 OSS/DashScope 并写数据库。每张照片在
描述、结构化分析和 embedding 全部成功后才原子提交，失败不会覆盖旧结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_ANALYSIS_VERSION = "v4"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return payload


def _split_ids(manifest_path: Path, split: str | None) -> set[str] | None:
    if not split:
        return None
    rows = _load_json(manifest_path).get("images")
    if not isinstance(rows, list):
        raise ValueError("manifest.images 必须是数组")
    result = {
        str(row.get("id"))
        for row in rows
        if isinstance(row, dict) and str(row.get("split")) == split
    }
    if not result:
        raise ValueError(f"manifest 中没有 split={split!r} 的图片")
    return result


def _select_records(args: argparse.Namespace) -> tuple[str, list[dict[str, Any]]]:
    payload = _load_json(args.import_map)
    user = payload.get("user")
    records = payload.get("records")
    if not isinstance(user, dict) or not isinstance(records, list):
        raise ValueError("import map 缺少 user/records")
    openid = str(user.get("wechat_openid", ""))
    if not openid.startswith("photo-eval-"):
        raise ValueError("安全限制：只允许 photo-eval-* 隔离测试用户")
    user_id = str(UUID(str(user.get("id"))))

    requested_ids = set(args.dataset_id or [])
    split_ids = _split_ids(args.manifest, args.split)
    selected: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        dataset_id = str(row.get("dataset_id", ""))
        if requested_ids and dataset_id not in requested_ids:
            continue
        if split_ids is not None and dataset_id not in split_ids:
            continue
        if args.synthetic_only and not (
            dataset_id.startswith("p-") and 139 <= int(dataset_id[2:]) <= 163
        ):
            continue
        selected.append(row)
    if not selected:
        raise ValueError("筛选后没有待处理照片")
    if requested_ids - {str(row.get("dataset_id")) for row in selected}:
        missing = sorted(requested_ids - {str(row.get("dataset_id")) for row in selected})
        raise ValueError(f"import map 中缺少 dataset_id: {missing}")
    return user_id, selected


async def _reprocess_one(
    user_id: str,
    row: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.photo import Photo
    from app.services.ai import (
        VL_ANALYSIS_PROMPT_VERSION,
        analyze_image,
        build_retrieval_text,
        describe_image,
        embed_text,
    )
    from app.services.oss import sign_get_url

    dataset_id = str(row["dataset_id"])
    photo_id = UUID(str(row["photo_id"]))
    expected_hash = str(row["sha256"])
    async with semaphore:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Photo).where(Photo.id == photo_id))
                photo = result.scalar_one_or_none()
                if photo is None:
                    raise ValueError("数据库中不存在该 photo_id")
                if str(photo.user_id) != user_id or str(photo.hash) != expected_hash:
                    raise ValueError("数据库记录与隔离用户/import map 不一致")
                oss_key = photo.oss_key

            image_url = sign_get_url(oss_key, ttl=600)
            description = await describe_image(image_url)
            analysis = await analyze_image(image_url)
            if analysis.parse_quality != "ok":
                raise RuntimeError(f"VL 结构化分析降级: {analysis.parse_quality}")
            if analysis.analysis_version != VL_ANALYSIS_PROMPT_VERSION:
                raise RuntimeError("VL 分析版本不一致")
            retrieval_text = build_retrieval_text(description, analysis)
            embedding = await embed_text(retrieval_text)
            if len(embedding) != 1024:
                raise RuntimeError(f"embedding 维数异常: {len(embedding)}")

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Photo).where(Photo.id == photo_id).with_for_update()
                )
                photo = result.scalar_one_or_none()
                if photo is None or str(photo.user_id) != user_id:
                    raise ValueError("提交前隔离用户校验失败")
                if str(photo.hash) != expected_hash:
                    raise ValueError("提交前图片哈希校验失败")
                photo.ai_description = description
                photo.ai_analysis = analysis.model_dump(exclude_none=True)
                photo.embedding = embedding
                await session.commit()
            return {
                "dataset_id": dataset_id,
                "photo_id": str(photo_id),
                "status": "updated",
                "analysis_version": analysis.analysis_version,
                "parse_quality": analysis.parse_quality,
                "retrieval_text_chars": len(retrieval_text),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "dataset_id": dataset_id,
                "photo_id": str(photo_id),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }


async def _run(args: argparse.Namespace) -> int:
    user_id, records = _select_records(args)
    plan = {
        "mode": "apply" if args.apply else "dry-run",
        "user_id": user_id,
        "analysis_version": _ANALYSIS_VERSION,
        "selected_count": len(records),
        "dataset_ids": [str(row["dataset_id"]) for row in records],
    }
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    from app.database import engine

    semaphore = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(
        *(_reprocess_one(user_id, row, semaphore) for row in records)
    )
    report = {
        **plan,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "updated": sum(row["status"] == "updated" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    await engine.dispose()
    return 1 if report["failed"] else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-map", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=_PROJECT_ROOT / "tests/eval/photo_manifest.json"
    )
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("--dataset-id", action="append")
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=_PROJECT_ROOT / "artifacts/photo-eval/reprocess-v4.json",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))
