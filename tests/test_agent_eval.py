"""评测器自身的回归测试。"""
from __future__ import annotations

from scripts.agent_eval import (
    TestCaseResult as EvalCaseResult,
    _parameter_matches,
    build_summary,
    evaluate_test_case,
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


def test_parameter_rules_validate_values_not_only_presence() -> None:
    assert _parameter_matches("帮我找狗的照片", {"contains_all": ["狗"], "excludes": ["猫"]})
    assert not _parameter_matches("帮我找猫和狗", {"contains_all": ["狗"], "excludes": ["猫"]})
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


def test_error_codes_are_globally_unique() -> None:
    from app.core.errors import AUTH_JWT_EXPIRED, INVALID_PARAMS, get_error_code

    assert INVALID_PARAMS.code != AUTH_JWT_EXPIRED.code
    assert get_error_code(INVALID_PARAMS.code) is INVALID_PARAMS
