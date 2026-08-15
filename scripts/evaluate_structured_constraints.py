"""Replay structured candidate validation over saved retrieval diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services.search_constraints import (  # noqa: E402
    evaluate_candidate_constraints,
    extract_structured_constraints,
)
from scripts.retrieval_eval import evaluate  # noqa: E402


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_predictions(paths: list[Path]) -> dict[str, dict[str, Any]]:
    predictions: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = _load(path)
        predictions.update(payload.get("predictions", {}))
    return predictions


def replay(
    queries: list[dict[str, Any]],
    retrieval: dict[str, Any],
    predictions: dict[str, dict[str, Any]],
    *,
    split: str | None,
    k: int,
) -> dict[str, Any]:
    selected = [
        query for query in queries if split is None or query.get("split") == split
    ]
    original_results = retrieval.get("results", retrieval)
    diagnostics = retrieval.get("diagnostics", {})
    filtered_results: dict[str, list[str]] = {}
    details = []
    kind_counts: Counter[str] = Counter()
    candidates_checked = 0
    candidates_rejected = 0

    for query in selected:
        query_id = query["id"]
        constraints = extract_structured_constraints(query["query"])
        kind_counts.update(item.kind for item in constraints)
        original = list(original_results.get(query_id, []))
        descriptions = {
            item.get("photo_id"): item.get("ai_description")
            for item in diagnostics.get(query_id, {}).get("items", [])
        }
        filtered = []
        rejected = []
        for photo_id in original:
            if not constraints:
                filtered.append(photo_id)
                continue
            if photo_id not in predictions:
                raise ValueError(f"missing frozen VL prediction for {photo_id}")
            candidates_checked += 1
            decision = evaluate_candidate_constraints(
                constraints,
                predictions[photo_id],
                descriptions.get(photo_id),
            )
            if decision.matches:
                filtered.append(photo_id)
            else:
                candidates_rejected += 1
                rejected.append(
                    {"photo_id": photo_id, "failed_kinds": list(decision.failed_kinds)}
                )
        filtered_results[query_id] = filtered
        details.append(
            {
                "id": query_id,
                "query": query["query"],
                "constraints": [item.as_dict() for item in constraints],
                "original": original,
                "filtered": filtered,
                "rejected": rejected,
            }
        )

    scored = evaluate(selected, filtered_results, k)
    return {
        "protocol": {
            "mode": "offline_replay",
            "split": split,
            "k": k,
            "candidate_source": "saved retrieval ranking",
            "analysis_source": "frozen VL predictions",
        },
        "constraint_summary": {
            "query_count": len(selected),
            "queries_with_constraints": sum(bool(item["constraints"]) for item in details),
            "constraint_kinds": dict(sorted(kind_counts.items())),
            "candidates_checked": candidates_checked,
            "candidates_rejected": candidates_rejected,
        },
        "evaluation": scored,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, nargs="+", required=True)
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()

    query_payload = _load(args.queries)
    report = replay(
        list(query_payload.get("queries", query_payload)),
        _load(args.results),
        _load_predictions(args.predictions),
        split=args.split,
        k=args.k,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "constraint_summary": report["constraint_summary"],
                "evaluation": report["evaluation"]["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
