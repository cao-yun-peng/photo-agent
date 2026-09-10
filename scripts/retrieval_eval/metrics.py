"""Pure offline retrieval scoring and paired query-family bootstrap.

Duplicate returns consume their original rank but earn relevance credit once.
Quality metrics describe the actual returned list, even on an operationally
failed attempt; operational errors are reported separately. In particular, an
empty failed/incomplete attempt never earns correct-empty credit. Selection
loss is query weighted: positive (1 - recall@5), or zero-result (1 - correct
empty), plus 0.5 for any labeled hard negative in the first five positions.
There is no additional operational penalty on positive-query selection loss.

All functions use only supplied frozen labels and result records. No provider,
database, network, file mutation, or production retrieval code is invoked.
"""

import math
import random
import statistics
from collections import Counter, defaultdict


INCOMPLETE_STOP_REASONS = frozenset({
    "verification_incomplete", "verification_unavailable", "deadline_exceeded",
    "budget_exhausted",
})
_POSITIVE_METRICS = (
    "recall_at_1", "recall_at_5", "hit_at_1", "hit_at_5",
    "precision_at_5", "mrr_at_5", "ndcg_at_5",
)
_COUNTERS = ("model_calls", "visual_calls", "input_tokens", "output_tokens")
_MEAN_METRICS = {name: name for name in _POSITIVE_METRICS}
_MEAN_METRICS.update({
    "zero_result_accuracy": "correct_empty",
    "hard_negative_hit_at_5": "hard_negative_hit_at_5",
    "mean_loss": "loss",
    "operational_error_rate": "operational_error",
    "duplicate_query_rate": "has_duplicates",
    "visual_trigger_rate": "visual_triggered",
    "latency_mean_ms": "latency_ms",
    "estimated_cost_per_query_cny": "estimated_cost_cny",
    "model_calls_per_query": "model_calls",
})


def _identifier(value, description):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{description} must be a nonempty string")
    return value


def _finite_nonnegative(value, description, *, integer=False):
    valid_type = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not valid_type or not math.isfinite(value) or value < 0:
        raise ValueError(f"{description} must be finite and nonnegative"
                         + (" integer" if integer else ""))
    return value


def _mean(values):
    values = list(values)
    return statistics.fmean(values) if values else None


def _percentile(values, probability):
    """Linearly interpolated empirical percentile (Hyndman-Fan type 7)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _aggregate(rows):
    positive = [r for r in rows if r["relevant_count"]]
    zero = [r for r in rows if not r["relevant_count"]]
    result = {
        "query_count": len(rows),
        "positive_query_count": len(positive),
        "zero_result_query_count": len(zero),
        "labeled_hard_negative_query_count": sum(r["hard_negative_count"] > 0 for r in rows),
        "family_count": len({r["family_id"] for r in rows if r["family_id"] is not None}),
        "missing_family_query_count": sum(r["family_id"] is None for r in rows),
    }
    for metric, row_key in _MEAN_METRICS.items():
        result[metric] = _mean(r[row_key] for r in rows if r[row_key] is not None)
    result.update({
        "correct_empty_count": sum(r["correct_empty"] for r in zero),
        "operational_error_count": sum(r["operational_error"] for r in rows),
        "duplicate_result_count": sum(r["duplicate_count"] for r in rows),
        "result_count": sum(r["result_count"] for r in rows),
        "result_count_mean": _mean(r["result_count"] for r in rows),
        "unique_result_count_mean": _mean(r["unique_result_count"] for r in rows),
        "latency_median_ms": statistics.median([r["latency_ms"] for r in rows]) if rows else None,
        "latency_p95_ms": _percentile([r["latency_ms"] for r in rows], .95),
        "estimated_cost_cny": sum(r["estimated_cost_cny"] for r in rows),
        "stop_reasons": dict(sorted(Counter(r["stop_reason"] for r in rows).items())),
        "error_codes": dict(sorted(Counter(r["error_code"] for r in rows if r["error_code"]).items())),
        "metric_denominators": {
            **{metric: len(positive) for metric in _POSITIVE_METRICS},
            "zero_result_accuracy": len(zero),
            **{metric: len(rows) for metric in _MEAN_METRICS
               if metric not in _POSITIVE_METRICS and metric != "zero_result_accuracy"},
        },
    })
    result.update({field: sum(r[field] for r in rows) for field in _COUNTERS})
    return result


def score_results(queries, results, corpus_ids):
    """Score one complete variant; return ``aggregate``, ``per_query``, ``slices``.

    Queries use ``id``, ``relevant_photo_ids``, ``hard_negative_photo_ids``,
    ``tags`` and optional ``family_id``. Results use ``query_id``,
    ``returned_photo_ids``, boolean ``success``, ``stop_reason``, ``variant``,
    ``latency_ms``, ``model_calls``, ``visual_calls``, ``input_tokens``,
    ``output_tokens``, ``estimated_cost_cny`` and optional ``error_code``.
    Unknown IDs, incomplete coverage and missing/invalid execution fields fail
    closed. Positive-only metrics and zero-only accuracy use separate explicit
    denominators; an absent population produces None, never an invented zero.
    Tag slices can overlap. Positive-count slices use exact counts as strings.
    """
    queries, results, corpus_ids = list(queries), list(results), list(corpus_ids)
    if not queries:
        raise ValueError("At least one query is required")
    for photo_id in corpus_ids:
        _identifier(photo_id, "corpus photo id")
    if len(set(corpus_ids)) != len(corpus_ids):
        raise ValueError("Duplicate corpus photo id")
    corpus = set(corpus_ids)
    by_id = {}
    for query in queries:
        query_id = _identifier(query["id"], "query id")
        if query_id in by_id:
            raise ValueError(f"Duplicate query id: {query_id}")
        for name in ("relevant_photo_ids", "hard_negative_photo_ids"):
            labels = query[name]
            if (not isinstance(labels, list) or any(not isinstance(p, str) for p in labels)
                    or not set(labels) <= corpus or len(set(labels)) != len(labels)):
                raise ValueError(f"Invalid or unknown photo IDs in {name}: {query_id}")
        relevant = set(query["relevant_photo_ids"])
        if relevant & set(query["hard_negative_photo_ids"]):
            raise ValueError(f"Overlapping positive and hard-negative labels: {query_id}")
        if "expected_empty" in query and (type(query["expected_empty"]) is not bool
                                           or query["expected_empty"] != (not relevant)):
            raise ValueError(f"Inconsistent expected_empty: {query_id}")
        if not isinstance(query.get("tags", []), list):
            raise ValueError(f"Query tags must be a list: {query_id}")
        for tag in query.get("tags", []):
            _identifier(tag, "query tag")
        if query.get("family_id") is not None:
            _identifier(query["family_id"], "query family id")
        by_id[query_id] = query

    result_by_id = {}
    for result in results:
        query_id = _identifier(result["query_id"], "result query id")
        if query_id in result_by_id:
            raise ValueError(f"Duplicate result query id: {query_id}")
        if query_id not in by_id:
            raise ValueError(f"Unknown result query id: {query_id}")
        result_by_id[query_id] = result
    missing = set(by_id) - set(result_by_id)
    if missing:
        raise ValueError(f"Missing results for queries: {', '.join(sorted(missing))}")

    rows, variants = [], set()
    for query_id, query in by_id.items():
        result = result_by_id[query_id]
        ranked = result["returned_photo_ids"]
        if not isinstance(ranked, list) or any(not isinstance(p, str) or p not in corpus for p in ranked):
            raise ValueError(f"Unknown/out-of-corpus returned photo id: {query_id}")
        if type(result["success"]) is not bool:
            raise ValueError(f"success must be boolean: {query_id}")
        variant = _identifier(result["variant"], "variant")
        variants.add(variant)
        stop_reason = _identifier(result["stop_reason"], "stop reason")
        error_code = result.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            raise ValueError(f"error_code must be a string or null: {query_id}")
        for name in _COUNTERS:
            _finite_nonnegative(result[name], name, integer=True)
        for name in ("latency_ms", "estimated_cost_cny"):
            _finite_nonnegative(result[name], name)
        positive, negative = set(query["relevant_photo_ids"]), set(query["hard_negative_photo_ids"])
        operational_error = int(not result["success"] or stop_reason in INCOMPLETE_STOP_REASONS or bool(error_code))
        row = {
            "query_id": query_id, "family_id": query.get("family_id"),
            "tags": list(dict.fromkeys(query.get("tags", []))), "variant": variant,
            "relevant_count": len(positive), "hard_negative_count": len(negative),
            "returned_photo_ids": list(ranked), "result_count": len(ranked),
            "unique_result_count": len(set(ranked)),
            "duplicate_count": len(ranked) - len(set(ranked)),
            "has_duplicates": int(len(set(ranked)) < len(ranked)),
            "hard_negative_hit_at_5": int(bool(set(ranked[:5]) & negative)),
            "success": result["success"], "stop_reason": stop_reason,
            "error_code": error_code, "operational_error": operational_error,
            "correct_empty": None if positive else int(not ranked and not operational_error),
            "visual_triggered": int(result["visual_calls"] > 0),
            **{name: result[name] for name in (*_COUNTERS, "latency_ms", "estimated_cost_cny")},
            **{name: None for name in _POSITIVE_METRICS},
        }
        if positive:
            for k in (1, 5):
                hits = len(set(ranked[:k]) & positive)
                row[f"recall_at_{k}"] = hits / len(positive)
                row[f"hit_at_{k}"] = int(hits > 0)
            row["precision_at_5"] = len(set(ranked[:5]) & positive) / 5
            seen, dcg, reciprocal = set(), 0.0, 0.0
            for rank, photo_id in enumerate(ranked[:5], 1):
                if photo_id in positive and photo_id not in seen:
                    dcg += 1 / math.log2(rank + 1)
                    reciprocal = reciprocal or 1 / rank
                seen.add(photo_id)
            ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(5, len(positive)) + 1))
            row["mrr_at_5"], row["ndcg_at_5"] = reciprocal, dcg / ideal
        base_loss = 1 - (row["recall_at_5"] if positive else row["correct_empty"])
        row["loss"] = base_loss + .5 * row["hard_negative_hit_at_5"]
        rows.append(row)
    if len(variants) != 1:
        raise ValueError("score_results requires exactly one variant per complete run")
    aggregate = _aggregate(rows)
    aggregate["variant"] = next(iter(variants))
    return {
        "aggregate": aggregate,
        "per_query": rows,
        "slices": {
            "by_tag": {tag: _aggregate([r for r in rows if tag in r["tags"]])
                       for tag in sorted({tag for r in rows for tag in r["tags"]})},
            "by_positive_count": {str(count): _aggregate([r for r in rows if r["relevant_count"] == count])
                                  for count in sorted({r["relevant_count"] for r in rows})},
        },
    }


def paired_cluster_bootstrap(queries, baseline_results, comparison_results, corpus_ids,
                             *, metrics=None, seed=20260908, n_resamples=2000):
    """Percentile 95% CI for comparison-minus-baseline paired mean differences.

    Draw exactly N query families with replacement in each replicate, retain
    every query in every drawn family, then recompute the query-weighted mean.
    Families must be explicitly labeled; there is no independent-query fallback.
    Metrics on positive/zero populations use only eligible queries. A replicate
    with no eligible queries is undefined and counted, not assigned zero. A
    single eligible family cannot establish between-family uncertainty and is
    reported as insufficient rather than with a misleading degenerate CI.
    """
    queries, corpus_ids = list(queries), list(corpus_ids)
    if isinstance(n_resamples, bool) or not isinstance(n_resamples, int) or n_resamples < 2:
        raise ValueError("n_resamples must be an integer of at least 2")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    for query in queries:
        _identifier(query.get("family_id"), "explicit query family_id required for cluster bootstrap")
    baseline = score_results(queries, baseline_results, corpus_ids)
    comparison = score_results(queries, comparison_results, corpus_ids)
    names = list(metrics) if metrics is not None else [
        "recall_at_5", "precision_at_5", "mrr_at_5", "ndcg_at_5",
        "zero_result_accuracy", "hard_negative_hit_at_5", "mean_loss",
        "operational_error_rate",
    ]
    if not names or len(set(names)) != len(names) or any(name not in _MEAN_METRICS for name in names):
        raise ValueError("metrics must be unique supported mean-metric names")
    families = sorted({query["family_id"] for query in queries})
    family_index = {family: index for index, family in enumerate(families)}
    sums = {name: [0.0] * len(families) for name in names}
    counts = {name: [0] * len(families) for name in names}
    for left, right in zip(baseline["per_query"], comparison["per_query"]):
        if left["query_id"] != right["query_id"]:
            raise ValueError("Paired result identity mismatch")
        index = family_index[left["family_id"]]
        for name in names:
            key = _MEAN_METRICS[name]
            if left[key] is not None and right[key] is not None:
                sums[name][index] += right[key] - left[key]
                counts[name][index] += 1
    draws = defaultdict(list)
    rng = random.Random(seed)
    for _ in range(n_resamples):
        selected = [rng.randrange(len(families)) for _ in families]
        for name in names:
            count = sum(counts[name][index] for index in selected)
            if count:
                draws[name].append(sum(sums[name][index] for index in selected) / count)
    intervals = {}
    for name in names:
        count = sum(counts[name])
        eligible_families = sum(c > 0 for c in counts[name])
        sufficient = eligible_families >= 2
        intervals[name] = {
            "baseline": baseline["aggregate"][name],
            "comparison": comparison["aggregate"][name],
            "difference": sum(sums[name]) / count if count else None,
            "ci95": [_percentile(draws[name], .025), _percentile(draws[name], .975)] if sufficient else None,
            "eligible_query_count": count,
            "eligible_family_count": eligible_families,
            "valid_resamples": len(draws[name]),
            "undefined_resamples": n_resamples - len(draws[name]),
            "status": "ok" if sufficient else "insufficient_eligible_families",
        }
    return {
        "method": "paired_query_family_cluster_percentile_bootstrap",
        "difference_direction": "comparison_minus_baseline", "confidence_level": .95,
        "seed": seed, "n_resamples": n_resamples, "family_count": len(families),
        "query_count": len(queries), "weighting": "equal_query_weight_after_family_resampling",
        "metrics": intervals,
    }
