"""Audit retrieval-query labels before calling the real search service."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def audit(queries_path: Path, manifest_path: Path) -> dict[str, Any]:
    query_payload = json.loads(queries_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    queries = query_payload.get("queries", query_payload)
    images = {image["id"]: image for image in manifest["images"]}

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    referenced: Counter[str] = Counter()

    for query in queries:
        query_id = query.get("id")
        text = str(query.get("query", "")).strip()
        relevant = list(query.get("relevant_photo_ids", []))
        forbidden = list(query.get("must_not_return", []))
        confusers = list(query.get("confuser_photo_ids", []))
        empty = bool(query.get("must_return_empty"))

        if not query_id or query_id in seen:
            errors.append({"type": "duplicate_or_missing_query_id", "query_id": query_id})
        seen.add(query_id)
        if not text:
            errors.append({"type": "empty_query_text", "query_id": query_id})
        if len(relevant) != len(set(relevant)):
            errors.append({"type": "duplicate_relevant_id", "query_id": query_id})
        overlap = sorted(set(relevant).intersection(forbidden))
        if overlap:
            errors.append(
                {"type": "relevant_forbidden_overlap", "query_id": query_id, "ids": overlap}
            )
        unknown = sorted((set(relevant) | set(forbidden) | set(confusers)) - images.keys())
        if unknown:
            errors.append({"type": "unknown_photo_id", "query_id": query_id, "ids": unknown})
        if empty and relevant:
            errors.append({"type": "empty_with_relevant", "query_id": query_id})
        if not empty and not relevant:
            errors.append({"type": "positive_without_relevant", "query_id": query_id})

        for photo_id in relevant:
            referenced[photo_id] += 1
            if photo_id in images and query.get("split") != images[photo_id].get("split"):
                warnings.append(
                    {
                        "type": "query_photo_split_mismatch",
                        "query_id": query_id,
                        "query_split": query.get("split"),
                        "photo_id": photo_id,
                        "photo_split": images[photo_id].get("split"),
                    }
                )

    split_counts = Counter(query.get("split") for query in queries)
    category_counts = Counter(
        images[photo_id]["category"]
        for query in queries
        for photo_id in query.get("relevant_photo_ids", [])
        if photo_id in images
    )
    negative_type_counts = Counter(
        query.get("negative_type", "unspecified")
        for query in queries
        if query.get("must_return_empty")
    )
    return {
        "ok": not errors,
        "query_count": len(queries),
        "positive_query_count": sum(bool(q.get("relevant_photo_ids")) for q in queries),
        "negative_query_count": sum(bool(q.get("must_return_empty")) for q in queries),
        "split_counts": dict(sorted(split_counts.items())),
        "referenced_photo_count": len(referenced),
        "category_counts": dict(sorted(category_counts.items())),
        "negative_type_counts": dict(sorted(negative_type_counts.items())),
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit retrieval-query labels")
    parser.add_argument("--queries", type=Path, default=Path("tests/eval/retrieval_queries.json"))
    parser.add_argument("--manifest", type=Path, default=Path("tests/eval/photo_manifest.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.queries, args.manifest)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
