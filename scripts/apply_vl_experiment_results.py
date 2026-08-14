"""将冻结 Prompt 的 VL 实验结果幂等写回测试用户，并补缩略图与向量。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.photo import Photo  # noqa: E402
from app.schemas.analysis import ImageAnalysis  # noqa: E402
from app.services import image as image_service  # noqa: E402
from app.services.oss import put_object, thumb_key_of  # noqa: E402
from app.services.quality import decide_storage, quality_gate  # noqa: E402

EMBEDDING_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "text-embedding/text-embedding"
)
EMBEDDING_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def load_frozen_predictions(paths: list[Path]) -> tuple[dict[str, Any], str]:
    predictions: dict[str, Any] = {}
    prompt_hashes: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("summary", {}).get("errors"):
            raise ValueError(f"实验含运行错误，拒绝写库: {path}")
        prompt_hashes.add(str(payload["prompt"]["sha256"]))
        for dataset_id, analysis in payload.get("predictions", {}).items():
            if dataset_id in predictions:
                raise ValueError(f"实验样本重复: {dataset_id}")
            predictions[dataset_id] = analysis
    if len(prompt_hashes) != 1:
        raise ValueError(f"实验 Prompt 不一致: {sorted(prompt_hashes)}")
    return predictions, prompt_hashes.pop()


def load_inputs(
    manifest_path: Path,
    import_map_path: Path,
    experiment_paths: list[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = (manifest_path.parent / manifest.get("image_root", "../..")).resolve()
    mapping_payload = json.loads(import_map_path.read_text(encoding="utf-8"))
    mapping = {row["dataset_id"]: row for row in mapping_payload["records"]}
    predictions, prompt_hash = load_frozen_predictions(experiment_paths)
    manifest_ids = {row["id"] for row in manifest["images"]}
    if set(mapping) != manifest_ids or set(predictions) != manifest_ids:
        raise ValueError(
            "manifest、import-map 与冻结预测必须覆盖完全相同的样本 ID"
        )
    rows = []
    for item in manifest["images"]:
        rows.append(
            {
                "dataset_id": item["id"],
                "path": (root / item["path"]).resolve(),
                "photo_id": UUID(mapping[item["id"]]["photo_id"]),
                "oss_key": mapping[item["id"]]["oss_key"],
                "analysis": predictions[item["id"]],
            }
        )
    return rows, mapping_payload["user"], prompt_hash


async def embed_batch(
    client: httpx.AsyncClient,
    texts: list[str],
) -> tuple[list[list[float]], dict[str, int]]:
    response = await client.post(
        EMBEDDING_URL,
        json={
            "model": settings.qwen_embedding_model,
            "input": {"texts": texts},
            "parameters": {"text_type": "document"},
        },
        headers={
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"Embedding HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    outputs = payload["output"]["embeddings"]
    outputs = sorted(outputs, key=lambda row: int(row.get("text_index", 0)))
    vectors = [row["embedding"] for row in outputs]
    if len(vectors) != len(texts) or any(len(vector) != 1024 for vector in vectors):
        raise RuntimeError("Embedding 批量响应数量或维度不一致")
    usage = {
        key: value
        for key, value in payload.get("usage", {}).items()
        if isinstance(value, int)
    }
    return vectors, usage


async def build_embeddings(
    rows: list[dict[str, Any]], batch_size: int
) -> tuple[dict[str, list[float]], dict[str, str], dict[str, int]]:
    vectors: dict[str, list[float]] = {}
    failures: dict[str, str] = {}
    usage_totals: Counter[str] = Counter()
    async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT) as client:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            texts = [str(row["analysis"].get("summary", "")).strip() for row in batch]
            try:
                output, usage = await embed_batch(client, texts)
                usage_totals.update(usage)
                for row, vector in zip(batch, output, strict=True):
                    vectors[row["dataset_id"]] = vector
            except Exception as exc:  # noqa: BLE001 - 保留 VL，按批记录降级
                error = f"{type(exc).__name__}: {exc}"
                for row in batch:
                    failures[row["dataset_id"]] = error
            print(f"embedding {min(start + batch_size, len(rows))}/{len(rows)}")
    return vectors, failures, dict(usage_totals)


async def build_thumbnails(
    rows: list[dict[str, Any]], concurrency: int
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    semaphore = asyncio.Semaphore(concurrency)
    completed: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}

    async def one(row: dict[str, Any]) -> None:
        async with semaphore:
            try:
                raw = await asyncio.to_thread(row["path"].read_bytes)
                processed = await asyncio.to_thread(image_service.process, raw, 512)
                thumb_key = thumb_key_of(row["oss_key"])
                await put_object(thumb_key, processed.thumb_bytes, "image/jpeg")
                completed[row["dataset_id"]] = {
                    "thumb_key": thumb_key,
                    "processed": processed,
                }
            except Exception as exc:  # noqa: BLE001
                failures[row["dataset_id"]] = f"{type(exc).__name__}: {exc}"

    await asyncio.gather(*(one(row) for row in rows))
    return completed, failures


async def write_database(
    rows: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
    thumbnails: dict[str, dict[str, Any]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    ids = [row["photo_id"] for row in rows]
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Photo).where(Photo.id.in_(ids)))
        photos = {photo.id: photo for photo in result.scalars().all()}
        if len(photos) != len(rows):
            raise ValueError("数据库照片数量与导入映射不一致")
        for row in rows:
            photo = photos[row["photo_id"]]
            analysis = ImageAnalysis.model_validate(row["analysis"])
            description = analysis.summary.strip()
            embedding = embeddings.get(row["dataset_id"])
            thumbnail = thumbnails.get(row["dataset_id"])
            gate = quality_gate(
                description=description,
                embedding=embedding,
                analysis=analysis,
            )
            decision = decide_storage(gate)
            photo.ai_description = description
            photo.ai_analysis = analysis.model_dump(exclude_none=True)
            photo.embedding = embedding
            if thumbnail:
                processed = thumbnail["processed"]
                photo.thumb_key = thumbnail["thumb_key"]
                photo.width = processed.width
                photo.height = processed.height
                photo.taken_at = processed.taken_at
                photo.location = processed.location
            if thumbnail is None and decision.status == "done":
                photo.status = "partial_done"
                photo.partial_reason = "thumbnail_failed"
            else:
                photo.status = decision.status
                photo.partial_reason = decision.partial_reason
            counts[photo.status] += 1
        await session.commit()
    return counts


async def run(args: argparse.Namespace) -> dict[str, Any]:
    rows, user, prompt_hash = load_inputs(
        args.manifest, args.import_map, args.experiment
    )
    embeddings, embedding_failures, usage = await build_embeddings(
        rows, args.embedding_batch_size
    )
    thumbnails, thumbnail_failures = await build_thumbnails(
        rows, args.thumbnail_concurrency
    )
    statuses = await write_database(rows, embeddings, thumbnails)
    result = {
        "version": "1.0.0",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "prompt_sha256": prompt_hash,
        "experiments": [str(path) for path in args.experiment],
        "summary": {
            "requested": len(rows),
            "embeddings": len(embeddings),
            "thumbnails": len(thumbnails),
            "statuses": dict(statuses),
        },
        "embedding_usage": usage,
        "embedding_failures": embedding_failures,
        "thumbnail_failures": thumbnail_failures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("tests/eval/photo_manifest.json"))
    parser.add_argument("--import-map", type=Path, default=Path("artifacts/photo-eval/import-map.json"))
    parser.add_argument("--experiment", type=Path, action="append", required=True)
    parser.add_argument("--embedding-batch-size", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--thumbnail-concurrency", type=int, default=4, choices=range(1, 9))
    parser.add_argument("--output", type=Path, default=Path("artifacts/vl-experiments/apply-v3.json"))
    return parser.parse_args()


async def async_main() -> int:
    try:
        result = await run(parse_args())
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        return 0 if not result["embedding_failures"] and not result["thumbnail_failures"] else 2
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
