"""Contract and review calibration cases require no paid providers."""

import pytest
from pydantic import ValidationError
from app.schemas.creative_plan import VisualReview
from app.services.provider_contract import image_contract, output_checks
from app.services.provider_ledger import summarize_calls
from tests.test_package_execution import png, plan


@pytest.mark.parametrize(
    "url",
    [
        "http://provider/v1",
        "https://key@provider/v1",
        "https://provider/v1?key=secret",
        "https://provider/v1#fragment",
    ],
)
def test_provider_contract_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        image_contract(url)


def test_contract_preserves_nonconforming_original_and_unknown_cost():
    assert (
        image_contract("https://provider.example/v1/")["upstream_model_verified"]
        is False
    )
    result = output_checks("1024x1024", [1254, 1254])
    assert result["issues"] and not result["dimensions_match"]
    assert summarize_calls([])["actual_yuan"] is None
    with pytest.raises(ValidationError):
        VisualReview(
            subject_preserved="true",
            style_aligned=True,
            title_correct=True,
            no_reference_copy=True,
            issues=[],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed,issues,expected",
    [
        (None, [], "passed"),
        ("subject_preserved", [], "needs_review"),
        ("style_aligned", [], "needs_review"),
        ("title_correct", [], "needs_review"),
        ("no_reference_copy", [], "needs_review"),
        (None, ["无法确定主体"], "needs_review"),
    ],
)
async def test_review_calibration_keeps_each_failure_visible(
    monkeypatch, failed, issues, expected
):
    from app.services import package_execution as execution

    monkeypatch.setattr(execution, "_is_mock", lambda: False)
    fields = dict(
        subject_preserved=True,
        style_aligned=True,
        title_correct=True,
        no_reference_copy=True,
        issues=issues,
    )
    if failed:
        fields[failed] = False

    async def visual(*args):
        return VisualReview(
            **fields, evidence={k: "可见证据" for k in fields if k != "issues"}
        )

    monkeypatch.setattr(execution, "visual_json", visual)
    result = await execution.review_output(
        {"size": "1024x1024", "plan": plan("").model_dump()}, [png()], png((1024, 1024))
    )
    assert result["status"] == expected
    if expected == "needs_review":
        assert result["issues"]


@pytest.mark.asyncio
async def test_missing_visual_evidence_never_passes(monkeypatch):
    from app.services import package_execution as execution

    monkeypatch.setattr(execution, "_is_mock", lambda: False)

    async def visual(*args):
        return VisualReview(
            subject_preserved=True,
            style_aligned=True,
            title_correct=True,
            no_reference_copy=True,
            issues=[],
        )

    monkeypatch.setattr(execution, "visual_json", visual)
    result = await execution.review_output(
        {"size": "1024x1024", "plan": plan("").model_dump()}, [png()], png((1024, 1024))
    )
    assert result["status"] == "needs_review"
    assert any("缺少逐项" in issue for issue in result["issues"])
