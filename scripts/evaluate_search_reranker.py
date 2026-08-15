"""对人工判同标签执行确定性离线回放，估计 Top-K 过滤的可达上界。

默认按 ``candidate_judgments`` 的顺序构造候选；提供 ``--results`` 时则在保存的真实
HTTP 排名上回放。未人工标注的候选一律视为 uncertain，不会被离线 oracle 删除。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.search_reranker import (  # noqa: E402
    RerankDecision,
    apply_rerank_decisions,
)
from scripts.retrieval_eval import evaluate, validate_queries  # noqa: E402


@dataclass(frozen=True)
class _PhotoRef:
    id: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_results(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("results", payload)
    if isinstance(raw, list):
        return {
            str(row.get("query_id", row.get("id"))): list(
                row.get("photo_ids", row.get("returned_photo_ids", []))
            )
            for row in raw
        }
    return {str(query_id): list(photo_ids) for query_id, photo_ids in raw.items()}


def validate_rerank_labels(queries: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    allowed = {"match", "contradiction", "uncertain"}
    for query in queries:
        seen: set[str] = set()
        relevant = set(query.get("relevant_photo_ids", []))
        for judgment in query.get("candidate_judgments", []):
            photo_id = str(judgment.get("photo_id", ""))
            verdict = str(judgment.get("verdict", ""))
            if not photo_id or photo_id in seen:
                errors.append(f"{query['id']} 候选 ID 为空或重复: {photo_id}")
            seen.add(photo_id)
            if verdict not in allowed:
                errors.append(f"{query['id']} 非法 verdict: {verdict}")
            if photo_id in relevant and verdict != "match":
                errors.append(f"{query['id']} 相关图片 {photo_id} 未标为 match")
            if photo_id not in relevant and verdict == "match":
                errors.append(f"{query['id']} 非相关图片 {photo_id} 被标为 match")
        missing = relevant - seen
        if missing:
            errors.append(
                f"{query['id']} 相关图片缺少 candidate_judgments: {sorted(missing)}"
            )
    return errors


def replay(
    queries: list[dict[str, Any]],
    baseline: dict[str, list[str]],
    *,
    top_k: int,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    baseline_map: dict[str, list[str]] = {}
    reranked_map: dict[str, list[str]] = {}
    for query in queries:
        query_id = query["id"]
        judgments = {
            str(item["photo_id"]): str(item["verdict"])
            for item in query.get("candidate_judgments", [])
        }
        ids = list(baseline.get(query_id) or judgments.keys())
        baseline_map[query_id] = ids
        scored = [
            (_PhotoRef(photo_id), 0.0, 0.0, 0.0, 1.0 - index * 0.01)
            for index, photo_id in enumerate(ids)
        ]
        decisions = [
            RerankDecision(
                candidate_key=f"c{index}",
                verdict=judgments.get(photo_id, "uncertain"),
                confidence=1.0 if photo_id in judgments else 0.0,
                rationale="human oracle"
                if photo_id in judgments
                else "unlabeled candidate",
            )
            for index, photo_id in enumerate(ids[:top_k])
        ]
        reranked, _ = apply_rerank_decisions(
            scored,
            decisions,
            top_k=top_k,
            reject_confidence=0.8,
        )
        reranked_map[query_id] = [str(item[0].id) for item in reranked]
    return baseline_map, reranked_map


def main() -> int:
    parser = argparse.ArgumentParser(description="离线回放 Top-K 人工判同标签")
    parser.add_argument(
        "--queries",
        type=Path,
        default=Path("tests/eval/retrieval_rerank_queries.json"),
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("tests/eval/photo_manifest.json")
    )
    parser.add_argument("--results", type=Path)
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/reranker-offline-replay.json")
    )
    args = parser.parse_args()

    payload = json.loads(args.queries.read_text(encoding="utf-8"))
    queries = [
        query
        for query in payload["queries"]
        if args.split is None or query.get("split") == args.split
    ]
    errors = [
        *validate_queries(queries, str(args.manifest)),
        *validate_rerank_labels(queries),
    ]
    baseline = _load_results(args.results)
    baseline_map, reranked_map = replay(queries, baseline, top_k=args.top_k)
    output = {
        "protocol": {
            "mode": "offline_human_oracle_replay",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "queries": str(args.queries),
            "queries_sha256": _sha256(args.queries),
            "manifest": str(args.manifest),
            "manifest_sha256": _sha256(args.manifest),
            "source_results": str(args.results) if args.results else None,
            "source_results_sha256": _sha256(args.results) if args.results else None,
            "split": args.split,
            "top_k": args.top_k,
            "unlabeled_policy": "uncertain_keep",
        },
        "validation_errors": errors,
        "baseline": evaluate(queries, baseline_map, args.top_k),
        "oracle_reranked": evaluate(queries, reranked_map, args.top_k),
        "baseline_results": baseline_map,
        "oracle_reranked_results": reranked_map,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "validation_errors": len(errors),
                "baseline": output["baseline"]["summary"],
                "oracle_reranked": output["oracle_reranked"]["summary"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
