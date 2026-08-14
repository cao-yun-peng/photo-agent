"""对隔离 OSS 图片集执行可复现的 Qwen-VL Prompt 实验。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.services.ai import parse_vl_response  # noqa: E402
from app.services.oss import sign_get_url  # noqa: E402
from scripts.offline_eval import (  # noqa: E402
    load_samples,
    score_prediction,
    summarize,
)

VL_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "multimodal-generation/generation"
)
VL_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_oss_mapping(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    failures = payload.get("summary", {}).get("failed", 0)
    if failures:
        raise ValueError(f"导入映射包含 {failures} 个失败项")
    mapping = {
        str(row["dataset_id"]): str(row["oss_key"])
        for row in payload.get("records", [])
    }
    if not mapping:
        raise ValueError("导入映射没有 records")
    return mapping


def extract_text(data: dict[str, Any]) -> str:
    choices = data["output"]["choices"]
    content = choices[0]["message"]["content"]
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return str(content).strip()


async def call_qwen_vl(
    client: httpx.AsyncClient,
    *,
    sample: dict[str, Any],
    oss_key: str,
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    queued_at = time.perf_counter()
    try:
        image_url = sign_get_url(oss_key, ttl=3600)
        payload = {
            "model": settings.qwen_vl_model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"image": image_url}, {"text": prompt}],
                    }
                ]
            },
            "parameters": {"result_format": "message", "max_tokens": 800},
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        async with semaphore:
            request_started = time.perf_counter()
            response = await client.post(VL_URL, json=payload, headers=headers)
        latency_ms = round((time.perf_counter() - request_started) * 1000)
        queue_wait_ms = round((request_started - queued_at) * 1000)
        if response.status_code != 200:
            return {
                "id": sample["id"],
                "error": f"HTTP {response.status_code}: {response.text[:300]}",
                "latency_ms": latency_ms,
                "queue_wait_ms": queue_wait_ms,
            }
        data = response.json()
        raw_text = extract_text(data)
        analysis = parse_vl_response(raw_text).model_dump(exclude_none=True)
        return {
            "id": sample["id"],
            "analysis": analysis,
            "raw_text": raw_text,
            "latency_ms": latency_ms,
            "queue_wait_ms": queue_wait_ms,
            "request_id": data.get("request_id"),
            "usage": data.get("usage", {}),
        }
    except Exception as exc:  # noqa: BLE001 - 单样本错误必须进入实验记录
        return {
            "id": sample["id"],
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.perf_counter() - queued_at) * 1000),
            "queue_wait_ms": None,
        }


def classify_failures(
    scored: list[dict[str, Any]], calls_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    failure_ids: dict[str, list[str]] = {
        "api_or_runtime": [],
        "parse": [],
        "scene": [],
        "persons": [],
        "objects": [],
        "ocr": [],
    }
    category_counts: Counter[str] = Counter()
    for result in scored:
        sample_id = result["id"]
        if "error" in result or "error" in calls_by_id.get(sample_id, {}):
            failure_ids["api_or_runtime"].append(sample_id)
            continue
        if not result.get("parse_ok"):
            failure_ids["parse"].append(sample_id)
        if not result.get("scene_ok"):
            failure_ids["scene"].append(sample_id)
        if not result.get("persons_ok"):
            failure_ids["persons"].append(sample_id)
        if float(result.get("object_recall", 0)) < 1.0:
            failure_ids["objects"].append(sample_id)
        if result.get("text_required") and float(result.get("ocr_recall", 0)) < 1.0:
            failure_ids["ocr"].append(sample_id)
        if any(
            (
                not result.get("scene_ok"),
                not result.get("persons_ok"),
                float(result.get("object_recall", 0)) < 1.0,
                bool(result.get("text_required"))
                and float(result.get("ocr_recall", 0)) < 1.0,
                not result.get("parse_ok"),
            )
        ):
            category_counts[str(result.get("category", "其他"))] += 1
    return {
        "failure_ids": failure_ids,
        "failure_count_by_category": dict(category_counts.most_common()),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if not settings.dashscope_api_key or settings.dashscope_api_key in {
        "sk-xxx",
        "please_set_dashscope_key",
    }:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")

    samples = load_samples(str(args.dataset), args.split)
    if args.limit is not None:
        samples = samples[: args.limit]
    oss_mapping = load_oss_mapping(args.import_map)
    missing = [sample["id"] for sample in samples if sample["id"] not in oss_mapping]
    if missing:
        raise ValueError(f"导入映射缺少 {len(missing)} 个样本: {missing[:5]}")
    aliases = json.loads(args.aliases.read_text(encoding="utf-8"))
    prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    semaphore = asyncio.Semaphore(args.concurrency)
    started_at = datetime.now(timezone.utc)
    wall_started = time.perf_counter()

    async with httpx.AsyncClient(timeout=VL_TIMEOUT) as client:
        tasks = [
            asyncio.create_task(
                call_qwen_vl(
                    client,
                    sample=sample,
                    oss_key=oss_mapping[sample["id"]],
                    prompt=prompt,
                    semaphore=semaphore,
                )
            )
            for sample in samples
        ]
        calls: list[dict[str, Any]] = []
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            calls.append(result)
            state = "ERROR" if "error" in result else "OK"
            print(f"[{completed:>3}/{len(tasks)}] {result['id']} {state} {result['latency_ms']}ms")

    calls_by_id = {call["id"]: call for call in calls}
    scored: list[dict[str, Any]] = []
    predictions: dict[str, dict[str, Any]] = {}
    for sample in samples:
        call = calls_by_id[sample["id"]]
        if "error" in call:
            scored.append({"id": sample["id"], "error": call["error"]})
            continue
        predictions[sample["id"]] = call["analysis"]
        scored.append(score_prediction(sample, call["analysis"], aliases))

    summary = summarize(scored)
    latencies = sorted(call["latency_ms"] for call in calls)
    usage_totals: Counter[str] = Counter()
    for call in calls:
        for key, value in call.get("usage", {}).items():
            if isinstance(value, int):
                usage_totals[key] += value
    experiment = {
        "version": "1.0.0",
        "experiment_id": args.experiment_id,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.qwen_vl_model,
        "split": args.split,
        "concurrency": args.concurrency,
        "retry_policy": "none",
        "dataset": {
            "path": str(args.dataset),
            "sha256": sha256_file(args.dataset),
            "sample_count": len(samples),
        },
        "prompt": {
            "path": str(args.prompt_file),
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "text": prompt,
        },
        "summary": summary,
        "runtime": {
            "wall_time_ms": round((time.perf_counter() - wall_started) * 1000),
            "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
            "latency_max_ms": max(latencies) if latencies else None,
            "usage_totals": dict(usage_totals),
        },
        "failure_analysis": classify_failures(scored, calls_by_id),
        "predictions": predictions,
        "calls": calls,
        "results": scored,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(experiment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    return experiment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--dataset", type=Path, default=Path("tests/eval/photo_manifest.json"))
    parser.add_argument("--import-map", type=Path, default=Path("artifacts/photo-eval/import-map.json"))
    parser.add_argument("--aliases", type=Path, default=Path("tests/eval/object_aliases.json"))
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--split", choices=["development", "validation", "test"], required=True)
    parser.add_argument("--concurrency", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    result = asyncio.run(run(parse_args()))
    print(json.dumps({"summary": result["summary"], "runtime": result["runtime"]}, ensure_ascii=False, indent=2))
    return 0 if result["summary"]["errors"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
