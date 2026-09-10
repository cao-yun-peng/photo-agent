"""Deterministic assertions. Missing observations are failures, never free passes."""

from collections import Counter
from statistics import median


def check_value(actual, expected):
    """Small declarative assertion language; no eval or executable dataset content."""
    if not isinstance(expected, dict) or "op" not in expected:
        return type(actual) is type(expected) and actual == expected
    if set(expected) != {"op", "value"}:
        raise ValueError("invalid assertion keys")
    op, value = expected["op"], expected["value"]
    if op == "contains":
        return isinstance(actual, (str, list)) and value in actual
    if op == "includes":
        return isinstance(actual, list) and all(v in actual for v in value)
    if op == "lte":
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and actual <= value
        )
    raise ValueError(f"unknown assertion operator: {op}")


def routing_score(expected, actual, mode="rule_router"):
    failures = []

    def check(key, ok):
        if not ok:
            failures.append(key)

    if mode == "rule_router":
        check("rule_outcome", (actual is None) == (expected["rule_outcome"] == "defer"))
        if expected["rule_outcome"] == "defer" or actual is None:
            return failures
    if actual is None:
        return ["missing_plan"]
    for key in ("intent", "relation", "needs_clarification"):
        check(key, check_value(actual.get(key), expected[key]))
    check("source", actual.get("source") in expected["allowed_sources"])
    for group in ("search", "feedback", "clarification"):
        target = expected.get(group, {})
        observed = actual.get(group) or {}
        for key, value in target.items():
            label = f"{group}.{key}"
            if key in {
                "query_all_terms",
                "query_any_terms",
                "query_forbidden_terms",
                "search_query_all_terms",
                "question_all_terms",
            }:
                field = (
                    "search_query"
                    if key.startswith("search_")
                    else "question"
                    if key.startswith("question_")
                    else "query"
                )
                text = observed.get(field)
                present = isinstance(text, str)
                if key.endswith("any_terms"):
                    ok = present and any(term in text for term in value)
                elif key.endswith("forbidden_terms"):
                    ok = present and all(term not in text for term in value)
                else:
                    ok = present and all(term in text for term in value)
                check(label, ok)
            elif key == "minimum_options":
                check(
                    label,
                    isinstance(observed.get("options"), list)
                    and len(observed["options"]) >= value,
                )
            else:
                check(label, key in observed and check_value(observed[key], value))
    return failures


def trajectory_score(expected, actual):
    allowed_keys = {
        "required_tools",
        "forbidden_tools",
        "before",
        "max_tool_calls",
        "state",
        "arguments",
        "terminal",
        "final_contains",
        "simulated_writes",
    }
    if not expected or set(expected) - allowed_keys:
        raise ValueError("empty or unknown trajectory assertion")
    failures = []
    invoked = [c["tool"] for c in actual["invocations"]]
    attempted = actual["attempted_tools"]
    for tool in expected.get("required_tools", []):
        if tool not in invoked:
            failures.append(f"required:{tool}")
    for tool in expected.get("forbidden_tools", []):
        if tool in invoked:
            failures.append(f"forbidden_execution:{tool}")
    for first, second in expected.get("before", []):
        if (
            first not in invoked
            or second not in invoked
            or invoked.index(first) >= invoked.index(second)
        ):
            failures.append(f"order:{first}:{second}")
    if len(attempted) > expected.get("max_tool_calls", len(attempted)):
        failures.append("max_tool_calls")
    for key, value in expected.get("state", {}).items():
        if key not in actual["state"] or not check_value(actual["state"][key], value):
            failures.append(f"state:{key}")
    for assertion in expected.get("arguments", []):
        if set(assertion) != {"tool", "index", "fields"}:
            raise ValueError("invalid argument assertion")
        calls = [c for c in actual["invocations"] if c["tool"] == assertion["tool"]]
        index = assertion["index"]
        if not isinstance(index, int) or index < 0 or index >= len(calls):
            failures.append(f"arguments:missing:{assertion['tool']}:{index}")
            continue
        for key, value in assertion["fields"].items():
            args = calls[index]["arguments"]
            if key not in args or not check_value(args[key], value):
                failures.append(f"arguments:{assertion['tool']}:{index}:{key}")
    for key in ("terminal", "simulated_writes"):
        if key in expected and not check_value(actual.get(key), expected[key]):
            failures.append(key)
    for term in expected.get("final_contains", []):
        if term not in actual.get("final", ""):
            failures.append("final_contains")
    if actual.get("execution_error"):
        failures.append("execution_error")
    return failures


def aggregate(rows):
    if not rows:
        raise ValueError("empty evaluation selection")
    count = len(rows)
    tags = sorted({tag for row in rows for tag in row["tags"]})
    latency = sorted(row["latency_ms"] for row in rows)
    return {
        "count": count,
        "passed": sum(not row["failures"] for row in rows),
        "pass_rate": sum(not row["failures"] for row in rows) / count,
        "failures_by_assertion": dict(
            Counter(f for row in rows for f in row["failures"])
        ),
        "safety_critical_failures": sum(
            bool(row["failures"]) and row["risk"] == "safety_critical" for row in rows
        ),
        "by_tag": {
            tag: {
                "count": sum(tag in r["tags"] for r in rows),
                "passed": sum(tag in r["tags"] and not r["failures"] for r in rows),
            }
            for tag in tags
        },
        "latency_ms": {
            "p50": median(latency),
            "p95": latency[min(count - 1, int(count * 0.95))],
        },
        "model_calls": sum(row.get("model_calls", 0) for row in rows),
        "model_call_rate": sum(row.get("model_calls", 0) > 0 for row in rows) / count,
        "case_stability": {
            case_id: {
                "runs": sum(r["id"] == case_id for r in rows),
                "passed": sum(r["id"] == case_id and not r["failures"] for r in rows),
            }
            for case_id in sorted({r["id"] for r in rows})
        },
        "tokens": sum(row.get("tokens", 0) for row in rows),
        "cost": None,
    }


def routing_metrics(rows):
    # Deferred rule cases measure delegation, not model intent accuracy.
    scored = [r for r in rows if r.get("score_intent")]
    labels = {r["expected_intent"] for r in scored} | {
        r.get("actual_intent") for r in scored
    }
    f1 = []
    for label in labels:
        if label is None:
            continue
        tp = sum(
            r["expected_intent"] == label == r.get("actual_intent") for r in scored
        )
        fp = sum(
            r["expected_intent"] != label == r.get("actual_intent") for r in scored
        )
        fn = sum(
            r["expected_intent"] == label != r.get("actual_intent") for r in scored
        )
        f1.append(2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0)
    return {
        "intent_sample_count": len(scored),
        "intent_macro_f1": sum(f1) / len(f1) if f1 else None,
        "relation_accuracy": sum(r.get("relation_ok", False) for r in scored)
        / len(scored)
        if scored
        else None,
        "dangerous_fast_path_count": sum(
            r.get("dangerous_fast_path", False) for r in rows
        ),
        "unnecessary_clarification_count": sum(
            r.get("unnecessary_clarification", False) for r in rows
        ),
        "missing_clarification_count": sum(
            r.get("missing_clarification", False) for r in rows
        ),
    }
