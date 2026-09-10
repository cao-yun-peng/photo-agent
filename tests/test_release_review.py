"""Negative controls for release evidence, latency and estimated costs."""

from datetime import datetime, timezone, timedelta
from hashlib import sha256
from copy import deepcopy
import pytest
from app.evaluation.release import (
    REQUIRED_GATES,
    review,
    source_hashes,
    latency_summary,
)
from app.services.generation_accounting import generation_cost, safe_usage
from app.services.image_gen import GenResult


def manifest(root):
    (root / "proof.txt").write_text("fixture evidence")
    evidence = [
        {
            "path": "proof.txt",
            "sha256": sha256((root / "proof.txt").read_bytes()).hexdigest(),
        }
    ]
    (root / "app").mkdir()
    (root / "app/main.py").write_text("# fixture")
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": source_hashes(root),
        "gates": {
            name: {
                "status": "passed",
                "mode": "live",
                "reviewer": "fixture reviewer",
                "acceptance": "synthetic gate control",
                "evidence": deepcopy(evidence),
                "critical_failures": 0,
                "coverage": 1,
                "basis": "reconciled",
                "samples_ms": [1] * 100,
                "p95_limit_ms": 2,
                "load_profile": "synthetic",
            }
            for name in REQUIRED_GATES
        },
    }


def test_complete_evidence_is_only_ready_for_owner_review(tmp_path):
    result = review(manifest(tmp_path), tmp_path)
    assert result["release_gate"] == "ready_for_owner_review"
    assert result["deployment_authorized"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "mock",
        "estimate",
        "incomplete",
        "safety",
        "latency",
        "nan",
        "stale",
        "source",
        "artifact",
        "escape",
    ],
)
def test_release_fails_closed(tmp_path, fault):
    data = manifest(tmp_path)
    if fault == "missing":
        del data["gates"]["engineering"]
    elif fault == "mock":
        data["gates"]["editing_quality"]["mode"] = "mock"
    elif fault == "estimate":
        data["gates"]["cost"]["basis"] = "estimate"
    elif fault == "incomplete":
        data["gates"]["cost"]["coverage"] = 0.9
    elif fault == "safety":
        data["gates"]["safety"]["critical_failures"] = 1
    elif fault == "latency":
        data["gates"]["latency"]["samples_ms"] = [1]
    elif fault == "nan":
        data["gates"]["latency"]["p95_limit_ms"] = float("nan")
    elif fault == "stale":
        data["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
    elif fault == "source":
        (tmp_path / "app/new.py").write_text("# changed")
    elif fault == "artifact":
        (tmp_path / "proof.txt").write_text("changed")
    elif fault == "escape":
        data["gates"]["safety"]["evidence"][0]["path"] = "../outside.txt"
    assert review(data, tmp_path)["release_gate"] == "blocked"


def test_cost_and_latency_never_invent_missing_values():
    cost = generation_cost(
        GenResult(0.3, "gpt-image-2", usage={"total_tokens": 30, "secret": "no"})
    )
    assert cost["actual_yuan"] is None and not cost["complete"]
    assert cost["usage"] == {"total_tokens": 30}
    assert generation_cost(GenResult(0, "mock"))["basis"] == "mock_zero"
    assert safe_usage({"input_tokens": True, "output_tokens": -1}) is None
    assert latency_summary(list(range(1, 101)))["p95"] == 95
    for invalid in ([], [float("nan")], [-1], [True]):
        with pytest.raises(ValueError):
            latency_summary(invalid)
