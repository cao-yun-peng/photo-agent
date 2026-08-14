"""对 Photo Agent 的检索排名结果计算 Recall@K、Precision@K、MRR 和误返回率。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_queries(path: str, split: str | None = None) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    queries = payload.get("queries", payload)
    return [query for query in queries if split is None or query.get("split") == split]


def _load_results(path: str) -> dict[str, list[str]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = payload.get("results", payload)
    if isinstance(raw, list):
        return {
            item.get("query_id", item.get("id")): list(
                item.get("photo_ids", item.get("returned_photo_ids", []))
            )
            for item in raw
        }
    return {str(query_id): list(ids) for query_id, ids in raw.items()}


def validate_queries(queries: list[dict[str, Any]], manifest_path: str) -> list[str]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    known = {image["id"] for image in manifest["images"]}
    errors, seen = [], set()
    for query in queries:
        query_id = query.get("id")
        if query_id in seen:
            errors.append(f"重复查询 ID: {query_id}")
        seen.add(query_id)
        references = set(query.get("relevant_photo_ids", [])) | set(
            query.get("must_not_return", [])
        )
        unknown = references - known
        if unknown:
            errors.append(f"{query_id} 引用了不存在的图片: {sorted(unknown)}")
        if query.get("must_return_empty") and query.get("relevant_photo_ids"):
            errors.append(f"{query_id} 同时要求空结果和相关图片")
    return errors


def evaluate(
    queries: list[dict[str, Any]],
    result_map: dict[str, list[str]],
    k: int = 5,
) -> dict[str, Any]:
    details = []
    for query in queries:
        query_id = query["id"]
        returned = result_map.get(query_id, [])[:k]
        relevant = set(query.get("relevant_photo_ids", []))
        forbidden = set(query.get("must_not_return", []))
        hits = [photo_id for photo_id in returned if photo_id in relevant]
        rank = next(
            (
                index
                for index, photo_id in enumerate(returned, 1)
                if photo_id in relevant
            ),
            None,
        )
        empty_query = bool(query.get("must_return_empty"))
        details.append(
            {
                "id": query_id,
                "query": query["query"],
                "returned": returned,
                "relevant": sorted(relevant),
                "recall_at_k": len(set(hits)) / len(relevant) if relevant else 1.0,
                "precision_at_k": len(hits) / len(returned)
                if returned
                else (1.0 if empty_query else 0.0),
                "reciprocal_rank": 1 / rank
                if rank
                else (1.0 if empty_query and not returned else 0.0),
                "forbidden_hits": sorted(forbidden.intersection(returned)),
                "empty_ok": not returned if empty_query else None,
                "missing_result": query_id not in result_map,
            }
        )
    total = len(details)
    positive = [detail for detail in details if detail["relevant"]]
    negative = [detail for detail in details if detail["empty_ok"] is not None]
    summary = {
        "total": total,
        "positive_queries": len(positive),
        "negative_queries": len(negative),
        "missing_results": sum(detail["missing_result"] for detail in details),
        "recall_at_k": sum(detail["recall_at_k"] for detail in positive) / len(positive)
        if positive
        else 1.0,
        "precision_at_k": sum(detail["precision_at_k"] for detail in positive)
        / len(positive)
        if positive
        else 1.0,
        "mrr": sum(detail["reciprocal_rank"] for detail in positive) / len(positive)
        if positive
        else 1.0,
        "empty_query_accuracy": sum(bool(detail["empty_ok"]) for detail in negative)
        / len(negative)
        if negative
        else 1.0,
        "forbidden_hit_rate": sum(bool(detail["forbidden_hits"]) for detail in details)
        / total
        if total
        else 0.0,
    }
    summary["gate_passed"] = (
        summary["missing_results"] == 0
        and summary["recall_at_k"] >= 0.85
        and summary["mrr"] >= 0.80
        and summary["empty_query_accuracy"] >= 0.90
        and summary["forbidden_hit_rate"] <= 0.05
    )
    return {"summary": summary, "results": details}


def print_summary(summary: dict[str, Any], k: int) -> None:
    print("\n========== 图片检索评测 ==========")
    print(f"查询: {summary['total']} | 缺失结果: {summary['missing_results']}")
    print(f"Recall@{k}: {summary['recall_at_k']:.2%}")
    print(f"Precision@{k}: {summary['precision_at_k']:.2%}")
    print(f"MRR: {summary['mrr']:.4f}")
    print(f"无结果准确率: {summary['empty_query_accuracy']:.2%}")
    print(f"禁返图片命中率: {summary['forbidden_hit_rate']:.2%}")
    print(f"门禁: {'PASS' if summary['gate_passed'] else 'FAIL'}")
    print("==================================\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Agent 图片检索评测")
    parser.add_argument("--queries", default="tests/eval/retrieval_queries.json")
    parser.add_argument("--manifest", default="tests/eval/photo_manifest.json")
    parser.add_argument("--results", help="query_id -> 排序后的 photo_id 列表")
    parser.add_argument("--output", default="artifacts/retrieval-eval-result.json")
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    queries = _load_queries(args.queries, args.split)
    errors = validate_queries(queries, args.manifest)
    if args.validate_only:
        print(
            json.dumps(
                {"ok": not errors, "total": len(queries), "errors": errors},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not errors else 1
    if errors:
        raise ValueError("; ".join(errors))
    if not args.results:
        raise ValueError("评分需要 --results；先从真实搜索接口导出排序后的 photo_id")
    output = evaluate(queries, _load_results(args.results), args.k)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(output["summary"], args.k)
    return 0 if output["summary"]["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
