import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.evaluation.contracts import RoutingCase, TrajectoryCase
from app.evaluation.runner import read_cases, run_routing, run_trajectory
from app.evaluation.scoring import (
    aggregate,
    check_value,
    routing_score,
    trajectory_score,
)

ROOT = Path(__file__).resolve().parents[1]
ROUTING = ROOT / "tests/eval/routing/turn_routing_v1.jsonl"
TRAJECTORY = ROOT / "tests/eval/agent/agent_trajectory_v1.jsonl"


def test_routing_defer_is_not_model_success():
    expected = read_cases(ROUTING, "development")[-1]["expected"]
    assert expected["rule_outcome"] == "defer"
    assert routing_score(expected, None) == []
    assert routing_score(expected, None, "router_system") == ["missing_plan"]


def test_routing_missing_and_wrong_fields_fail():
    case = read_cases(ROUTING, "development")[2]
    result = asyncio.run(run_routing(case))
    assert result["failures"] == []
    actual = deepcopy(result["actual"])
    actual["search"]["query"] = "完全不同"
    actual["search"]["complete_result_set"] = None
    actual["source"] = "rule_fallback"
    assert set(routing_score(case["expected"], actual)) >= {
        "search.query_all_terms",
        "search.complete_result_set",
        "source",
    }


def test_unknown_dataset_expectations_fail_closed():
    case = read_cases(ROUTING, "development")[0]
    case["expected"]["misspelled_assertion"] = True
    with pytest.raises(ValidationError):
        RoutingCase.model_validate(case)
    case = read_cases(TRAJECTORY, "development")[0]
    case["turns"][0]["expected"]["misspelled_assertion"] = True
    with pytest.raises(ValidationError):
        TrajectoryCase.model_validate(case)


def test_assertion_language_cannot_execute_code():
    with pytest.raises(ValueError):
        check_value([], {"op": "eval", "value": "print('bad')"})
    assert not check_value(None, False)
    assert not check_value([], {"op": "contains", "value": "missing"})
    with pytest.raises(ValueError):
        aggregate([])


def test_trajectory_negative_controls():
    actual = {
        "invocations": [{"tool": "apply_skill", "arguments": {"photo_id": "wrong"}}],
        "attempted_tools": ["apply_skill", "apply_skill"],
        "state": {},
        "terminal": "error",
        "simulated_writes": 1,
        "final": "错误",
    }
    expected = {
        "required_tools": ["search_photos"],
        "forbidden_tools": ["apply_skill"],
        "before": [["search_photos", "apply_skill"]],
        "max_tool_calls": 1,
        "state": {"confirmed_photo_id": "p1"},
        "terminal": "final",
        "simulated_writes": 0,
        "arguments": [
            {"tool": "apply_skill", "index": 0, "fields": {"photo_id": "p1"}}
        ],
    }
    errors = trajectory_score(expected, actual)
    assert len(errors) == 8
    with pytest.raises(ValueError):
        trajectory_score({"unknown": True}, actual)


def test_multi_turn_fixture_uses_real_feedback_and_exclusions():
    case = read_cases(TRAJECTORY, "development")[0]
    case["turns"][1]["expected"]["max_tool_calls"] = (
        2  # feedback and continuation are separate public actions
    )
    # Legacy seed deliberately used no decisions because routing executed both turns.
    # Adapt the script to the new public tool contract; keep the original oracle.
    case["turns"][0]["decisions"] = [
        {
            "tool_calls": [
                {
                    "id": "search",
                    "function": {
                        "name": "search_photos",
                        "arguments": '{"query":"猫","change":"new"}',
                    },
                }
            ]
        }
    ]
    case["turns"][1]["decisions"] = [
        {
            "tool_calls": [
                {
                    "id": "feedback",
                    "function": {
                        "name": "feedback_results",
                        "arguments": '{"kind":"reject_items","photo_ids":["10000000-0000-0000-0000-000000000002"],"finish_turn":false}',
                    },
                }
            ]
        }
    ]
    case["turns"][1]["decisions"][0]["tool_calls"].append(
        {"id": "more", "function": {"name": "continue_search", "arguments": "{}"}}
    )
    for response in case["tool_fixtures"]["search_photos"]:
        response["_search_plan_id"] = "fixture-plan"
        response["next_cursor"] = "next"
    result = asyncio.run(run_trajectory(case))
    assert result["failures"] == []
    assert len(result["turns"]) == 2
    assert result["turns"][1]["actual"]["state"]["rejected_photo_ids"]
    assert result["model_calls"] == result["simulated_writes"] == 0


def test_missing_fixture_never_silently_passes():
    case = read_cases(TRAJECTORY, "development")[0]
    case["tool_fixtures"] = {}
    result = asyncio.run(run_trajectory(case))
    assert any("execution_error" in f for f in result["failures"])


def test_live_eval_model_does_not_receive_expected_answers(monkeypatch):
    from app.services import agent

    case = next(
        c
        for c in read_cases(TRAJECTORY, "development")
        if "skill-recommendation" in c["id"]
    )
    calls = []

    async def model(messages, tools):
        calls.append(json.dumps(messages, ensure_ascii=False))
        if len(calls) == 1:
            return case["turns"][0]["decisions"][0], {"total_tokens": 11}
        return {"content": "可以试试针织", "tool_calls": []}, {"total_tokens": 7}

    monkeypatch.setattr(agent, "_llm_decide", model)
    result = asyncio.run(run_trajectory(case, live=True))
    assert result["failures"] == []
    assert result["tokens"] == 18
    assert all(
        '"expected"' not in message and "required_tools" not in message
        for message in calls
    )


def test_cli_protects_test_and_rejects_implicit_live():
    for args in [("--split", "test"), ("--mode", "router_system")]:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/eval_agent.py"),
                "routing",
                *args,
                "--output",
                "unused.json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2


def test_failure_report_exit_and_metadata(tmp_path):
    path = tmp_path / "baseline.json"
    dataset = tmp_path / "case.jsonl"
    case = read_cases(ROUTING, "development")[0]
    case["expected"]["intent"] = "photo_search"  # Deliberately wrong oracle.
    dataset.write_text(json.dumps(case) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/eval_agent.py"),
            "routing",
            "--output",
            str(path),
            "--dataset",
            str(dataset),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1  # Scoring failure must fail the command.
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["gate"] == "baseline_failed"
    assert report["summary"]["count"] == 1
    assert report["summary"]["model_calls"] == 0
    assert report["file_sha256"] and report["dataset_sha256"]
    assert report["release_gate"] == "not_evaluated"
