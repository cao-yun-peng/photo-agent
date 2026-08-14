"""Calibrate a top-1 semantic rejection threshold on retrieval development data."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _queries(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    return list(payload.get("queries", payload))


def _top_score(
    diagnostics: dict[str, Any], query_id: str, score_field: str
) -> float:
    items = diagnostics.get(query_id, {}).get("items", [])
    if not items:
        raise ValueError(f"{query_id} has no retrieval diagnostics")
    value = items[0].get(score_field)
    if value is None:
        raise ValueError(f"{query_id} top result has no {score_field}")
    return float(value)


def build_rows(
    queries: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    *,
    score_field: str,
) -> list[dict[str, Any]]:
    rows = []
    for query in queries:
        is_positive = bool(query.get("relevant_photo_ids"))
        is_negative = bool(query.get("must_return_empty"))
        if not is_positive and not is_negative:
            continue
        rows.append(
            {
                "id": query["id"],
                "query": query["query"],
                "split": query.get("split"),
                "kind": "positive" if is_positive else "negative",
                "negative_type": query.get("negative_type"),
                "top_score": _top_score(diagnostics, query["id"], score_field),
            }
        )
    return rows


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "median": median(values),
        "max": max(values),
        "mean": mean(values),
    }


def score_threshold(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    positives = [row for row in rows if row["kind"] == "positive"]
    negatives = [row for row in rows if row["kind"] == "negative"]
    accepted_positive = [row for row in positives if row["top_score"] >= threshold]
    rejected_negative = [row for row in negatives if row["top_score"] < threshold]
    positive_acceptance = (
        len(accepted_positive) / len(positives) if positives else None
    )
    negative_rejection = len(rejected_negative) / len(negatives) if negatives else None
    rates = [rate for rate in (positive_acceptance, negative_rejection) if rate is not None]
    by_negative_type: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in negatives:
        grouped[row.get("negative_type") or "unspecified"].append(row)
    for kind, group in sorted(grouped.items()):
        rejected = [row for row in group if row["top_score"] < threshold]
        by_negative_type[kind] = {
            "count": len(group),
            "rejected": len(rejected),
            "rejection_rate": len(rejected) / len(group),
        }
    return {
        "threshold": threshold,
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "positive_acceptance": positive_acceptance,
        "negative_rejection": negative_rejection,
        "balanced_accuracy": mean(rates) if rates else None,
        "false_negative_ids": [
            row["id"] for row in positives if row["top_score"] < threshold
        ],
        "false_positive_ids": [
            row["id"] for row in negatives if row["top_score"] >= threshold
        ],
        "negative_types": by_negative_type,
    }


def candidate_thresholds(rows: list[dict[str, Any]]) -> list[float]:
    scores = sorted({float(row["top_score"]) for row in rows})
    if not scores:
        raise ValueError("no scored queries")
    epsilon = 1e-9
    return [scores[0] - epsilon, *[(a + b) / 2 for a, b in zip(scores, scores[1:])], scores[-1] + epsilon]


def select_threshold(
    rows: list[dict[str, Any]], *, min_positive_acceptance: float | None
) -> dict[str, Any]:
    candidates = [score_threshold(rows, value) for value in candidate_thresholds(rows)]
    if min_positive_acceptance is not None:
        candidates = [
            candidate
            for candidate in candidates
            if candidate["positive_acceptance"] is not None
            and candidate["positive_acceptance"] >= min_positive_acceptance
        ]
    if not candidates:
        raise ValueError("no threshold satisfies the positive-acceptance constraint")
    return max(
        candidates,
        key=lambda item: (
            item["balanced_accuracy"],
            item["negative_rejection"] if item["negative_rejection"] is not None else -1,
            item["positive_acceptance"] if item["positive_acceptance"] is not None else -1,
            -item["threshold"],
        ),
    )


def calibrate(
    positive_queries: list[dict[str, Any]],
    positive_diagnostics: dict[str, Any],
    negative_queries: list[dict[str, Any]],
    negative_diagnostics: dict[str, Any],
    *,
    score_field: str = "score_semantic",
    min_positive_acceptance: float = 0.95,
    target_negative_rejection: float = 0.90,
) -> dict[str, Any]:
    positive_rows = build_rows(
        positive_queries, positive_diagnostics, score_field=score_field
    )
    negative_rows = build_rows(
        negative_queries, negative_diagnostics, score_field=score_field
    )
    development_rows = [
        row
        for row in positive_rows
        if row["split"] == "development" and row["kind"] == "positive"
    ] + negative_rows

    constrained = select_threshold(
        development_rows, min_positive_acceptance=min_positive_acceptance
    )
    unconstrained = select_threshold(development_rows, min_positive_acceptance=None)

    frozen = {}
    frozen_warnings = []
    for split in ("validation", "test"):
        split_rows = [row for row in positive_rows if row["split"] == split]
        frozen[split] = score_threshold(split_rows, constrained["threshold"])
        negative_count = frozen[split]["negative_count"]
        if negative_count < 10:
            frozen_warnings.append(
                f"{split} has only {negative_count} negative query; "
                "its rejection rate is underpowered"
            )

    negative_types = defaultdict(list)
    for row in negative_rows:
        negative_types[row.get("negative_type") or "unspecified"].append(
            row["top_score"]
        )
    suitable = (
        constrained["positive_acceptance"] >= min_positive_acceptance
        and constrained["negative_rejection"] >= target_negative_rejection
    )
    return {
        "protocol": {
            "calibration_split": "development",
            "frozen_splits": ["validation", "test"],
            "score_field": score_field,
            "decision_rule": "return empty when top_score < threshold",
            "min_positive_acceptance": min_positive_acceptance,
            "target_negative_rejection": target_negative_rejection,
        },
        "score_distributions": {
            "development_positive": _summary(
                [
                    row["top_score"]
                    for row in development_rows
                    if row["kind"] == "positive"
                ]
            ),
            "development_negative": _summary(
                [
                    row["top_score"]
                    for row in development_rows
                    if row["kind"] == "negative"
                ]
            ),
            "development_negative_by_type": {
                kind: _summary(values) for kind, values in sorted(negative_types.items())
            },
        },
        "constrained_selection": constrained,
        "unconstrained_selection": unconstrained,
        "frozen_evaluation": frozen,
        "warnings": frozen_warnings,
        "single_threshold_suitable": suitable,
        "conclusion": (
            "A single semantic threshold meets the development targets."
            if suitable
            else "A single semantic threshold cannot meet both development targets; use contradiction/OCR checks or a learned reranker."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive-queries", type=Path, required=True)
    parser.add_argument("--positive-results", type=Path, required=True)
    parser.add_argument("--negative-queries", type=Path, required=True)
    parser.add_argument("--negative-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--score-field", default="score_semantic")
    parser.add_argument("--min-positive-acceptance", type=float, default=0.95)
    parser.add_argument("--target-negative-rejection", type=float, default=0.90)
    args = parser.parse_args()

    positive_results = _load_json(args.positive_results)
    negative_results = _load_json(args.negative_results)
    report = calibrate(
        _queries(args.positive_queries),
        positive_results.get("diagnostics", {}),
        _queries(args.negative_queries),
        negative_results.get("diagnostics", {}),
        score_field=args.score_field,
        min_positive_acceptance=args.min_positive_acceptance,
        target_negative_rejection=args.target_negative_rejection,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
