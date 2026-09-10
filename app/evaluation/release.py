"""Evidence-backed release review. A passing review is never deployment approval."""

from datetime import datetime, timezone
from hashlib import sha256
import math
from pathlib import Path


REQUIRED_GATES = {
    "engineering",
    "safety",
    "routing_quality",
    "retrieval_quality",
    "editing_quality",
    "full_stack",
    "cost",
    "latency",
    "operations",
}
LIVE_GATES = {
    "routing_quality",
    "retrieval_quality",
    "editing_quality",
    "cost",
    "latency",
}
SOURCE_DIRS = (
    "app",
    "alembic",
    "scripts",
    "tests",
    "web/app",
    "web/features",
    "web/lib",
    "web/components",
    "web/e2e",
    ".github/workflows",
)


def source_hashes(root: Path) -> dict[str, str]:
    paths = [
        p
        for directory in SOURCE_DIRS
        for p in (root / directory).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".json", ".jsonl", ".tsx", ".ts", ".css", ".yml"}
        and "__pycache__" not in p.parts
    ]
    paths += [
        p
        for pattern in (
            "web/*.ts",
            "web/*.js",
            "web/*.json",
            "Dockerfile*",
            "docker-compose*.yml",
        )
        for p in root.glob(pattern)
        if p.is_file()
    ]
    paths += [
        root / p
        for p in (
            "requirements.txt",
            ".env.example",
            "alembic.ini",
            "web/package.json",
            "web/package-lock.json",
        )
        if (root / p).is_file()
    ]
    return {
        p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in sorted(set(paths))
    }


def latency_summary(samples: list[float]) -> dict:
    if not samples or any(
        type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in samples
    ):
        raise ValueError("latency needs finite nonnegative samples")
    values = sorted(samples)
    return {
        "count": len(values),
        "method": "nearest_rank",
        "unit": "ms",
        "p50": values[math.ceil(len(values) * 0.5) - 1],
        "p95": values[math.ceil(len(values) * 0.95) - 1],
        "max": values[-1],
    }


def review(manifest: dict, root: Path, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    blockers = []
    if manifest.get("schema_version") != 1:
        blockers.append("unsupported_schema")
    if not manifest.get("source_sha256") or manifest["source_sha256"] != source_hashes(
        root
    ):
        blockers.append("source_missing_or_changed")
    try:
        age = (now - datetime.fromisoformat(manifest["created_at"])).total_seconds()
        if not 0 <= age <= 86400:
            blockers.append("evidence_expired_or_future")
    except (KeyError, ValueError, TypeError):
        blockers.append("invalid_evidence_time")
    gates = manifest.get("gates", {})
    results = {}
    for name in sorted(REQUIRED_GATES):
        gate = gates.get(name, {})
        reasons = []
        if gate.get("status") != "passed":
            reasons.append("not_passed")
        if name in LIVE_GATES and gate.get("mode") != "live":
            reasons.append("real_evidence_required")
        if not gate.get("reviewer") or not gate.get("acceptance"):
            reasons.append("review_or_acceptance_missing")
        evidence = gate.get("evidence", [])
        if not evidence:
            reasons.append("evidence_missing")
        for entry in evidence:
            try:
                path = (root / entry["path"]).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file():
                    raise ValueError("invalid evidence path")
                if sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                    reasons.append("evidence_changed")
            except (KeyError, ValueError, OSError, TypeError):
                reasons.append("evidence_invalid")
        if name == "safety" and gate.get("critical_failures") != 0:
            reasons.append("critical_failures_unknown_or_nonzero")
        if name == "cost" and (
            gate.get("basis") != "reconciled" or gate.get("coverage") != 1
        ):
            reasons.append("incomplete_cost_accounting")
        if name == "latency":
            try:
                summary = latency_summary(gate["samples_ms"])
                threshold = gate["p95_limit_ms"]
                if (
                    len(gate["samples_ms"]) < 100
                    or type(threshold) not in (int, float)
                    or not math.isfinite(threshold)
                    or threshold <= 0
                    or summary["p95"] > threshold
                    or not gate.get("load_profile")
                ):
                    reasons.append("latency_gate_failed")
            except (KeyError, ValueError, TypeError):
                reasons.append("latency_data_missing_or_invalid")
        results[name] = {
            "status": "blocked" if reasons else "passed",
            "reasons": sorted(set(reasons)),
            "note": gate.get("note", ""),
        }
        blockers.extend(f"{name}:{reason}" for reason in sorted(set(reasons)))
    return {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "release_gate": "blocked" if blockers else "ready_for_owner_review",
        "deployment_authorized": False,
        "blockers": blockers,
        "gates": results,
    }
