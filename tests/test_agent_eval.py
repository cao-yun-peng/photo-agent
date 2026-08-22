"""评测器自身的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scripts.agent_eval import (
    TestCaseResult as EvalCaseResult,
    _parameter_matches,
    build_summary,
    evaluate_test_case,
    run_single_test,
    validate_dataset,
    validate_real_connectivity,
)


def _result(*, score: float, threshold: float, test_id: str = "TC-X") -> EvalCaseResult:
    result = EvalCaseResult(test_id, "D1", "P0", threshold)
    result.score_overall = score
    return result


def test_summary_uses_each_case_threshold() -> None:
    passing = _result(score=0.85, threshold=0.8, test_id="TC-PASS")
    failing = _result(score=0.85, threshold=0.9, test_id="TC-FAIL")

    summary = build_summary(
        [passing, failing],
        {
            "dimensions": {"D1_意图识别": {"weight": 1.0, "pass_threshold": 0.8}},
            "overall_pass_threshold": 0.8,
        },
        mode="real",
    )

    assert summary["passed"] == 1
    assert summary["failed"] == 1


def test_summary_builds_route_confusion_matrix() -> None:
    correct = _result(score=1.0, threshold=0.8, test_id="TC-R1")
    correct.expected_route = "search"
    correct.actual_route = "search"
    confused = _result(score=1.0, threshold=0.8, test_id="TC-R2")
    confused.expected_route = "search"
    confused.actual_route = "complex_agent"

    summary = build_summary(
        [correct, confused],
        {"dimensions": {"D1": {"weight": 1.0, "pass_threshold": 0.8}}},
        mode="real",
    )

    assert summary["route_confusion_matrix"] == {
        "search": {"search": 1, "complex_agent": 1}
    }
    assert summary["metrics"]["route_accuracy"] == 0.5


def test_parameter_rules_validate_values_not_only_presence() -> None:
    assert _parameter_matches(
        "帮我找狗的照片", {"contains_all": ["狗"], "excludes": ["猫"]}
    )
    assert not _parameter_matches(
        "帮我找猫和狗", {"contains_all": ["狗"], "excludes": ["猫"]}
    )
    assert not _parameter_matches("", {"not_empty": True})


def test_runtime_error_can_never_pass() -> None:
    result = EvalCaseResult("TC-ERR", "D1", "P0", 0.5)
    result.error = "tool crashed"
    result.final_status = "error"

    evaluated = evaluate_test_case(
        result,
        {
            "dimension": "D1",
            "expected": {"expected_tools": [], "expected_final_status": "error"},
        },
    )

    assert not evaluated.passed
    assert evaluated.score_overall == 0.0


def test_natural_language_clarification_is_valid_alternative() -> None:
    result = EvalCaseResult("TC-CLARIFY", "D5", "P0", 0.7)
    result.final_status = "completed"
    result.final_message = "你想按时间、地点还是照片类型来找？"

    evaluated = evaluate_test_case(
        result,
        {
            "dimension": "D5",
            "expected": {
                "expected_tools": ["ask_clarification"],
                "expected_tools_any_of": [[]],
                "expected_final_status": "clarified",
                "expected_final_status_any_of": ["clarified", "completed"],
                "expected_result_contains_any": ["时间", "地点", "类型"],
            },
        },
    )

    assert evaluated.score_tool_selection == 1.0
    assert evaluated.score_final_status == 1.0
    assert evaluated.score_content == 1.0
    assert evaluated.passed


def test_dataset_matches_current_agent_policy() -> None:
    dataset_path = Path("tests/eval/agent_eval_dataset.json")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))

    validate_dataset(dataset)

    assert dataset["version"] == "1.2.0"
    assert len(dataset["test_cases"]) == 52


def test_token_budget_is_scored() -> None:
    result = EvalCaseResult("TC-TOKEN", "D10", "P0", 0.9)
    result.final_status = "completed"
    result.final_message = "Token 预算已用完"
    result.total_tokens = 11

    evaluated = evaluate_test_case(
        result,
        {
            "dimension": "D10",
            "expected": {
                "expected_tools": [],
                "expected_final_status": "completed",
                "max_total_tokens": 10,
            },
        },
    )

    assert evaluated.score_budget == 0.0
    assert any("Token 超限" in note for note in evaluated.evaluation_notes)


def test_error_codes_are_globally_unique() -> None:
    from app.core.errors import AUTH_JWT_EXPIRED, INVALID_PARAMS, get_error_code

    assert INVALID_PARAMS.code != AUTH_JWT_EXPIRED.code
    assert get_error_code(INVALID_PARAMS.code) is INVALID_PARAMS


@pytest.mark.asyncio
async def test_degraded_llm_uses_stub_and_is_counted_as_infra_error() -> None:
    from app.services.circuit_breaker import ServiceDegradedError

    test_case = {
        "id": "TC-INFRA",
        "dimension": "D1",
        "priority": "P0",
        # 普通找图已由确定性快路径处理；这里用复杂请求专门验证 Agent LLM 熔断。
        "user_query": "帮我处理一下相册",
        "context": {"photos_available": True},
        "expected": {
            "expected_tools": ["search_photos", "final_answer"],
            "expected_final_status": "completed",
        },
        "rubric": {"pass_threshold": 0.8},
    }
    with patch(
        "app.services.agent._llm_decide",
        new=AsyncMock(
            side_effect=ServiceDegradedError("dashscope_chat", "circuit open")
        ),
    ):
        result = await run_single_test(test_case, {"photos": []}, mode="real")

    assert result.error_type == "model_service_degraded"
    assert result.error
    assert not result.passed
    assert any(call["tool"] == "browse_candidates" for call in result.tool_calls)


@pytest.mark.asyncio
async def test_real_connectivity_preflight_reports_exception_type() -> None:
    with patch(
        "app.services.agent._llm_decide",
        new=AsyncMock(side_effect=TimeoutError()),
    ):
        with pytest.raises(ValueError, match="TimeoutError"):
            await validate_real_connectivity()
