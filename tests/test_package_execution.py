import base64
import io
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.schemas.creative_plan import CreativePlan, PackageOptions
from app.services.package_execution import (
    title_policy,
    build_snapshot,
    PackageExecutionError,
    normalize_image,
    review_output,
)


def png(size=(48, 32), color="orange"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def plan(title="Warm Light"):
    return CreativePlan(
        observation="一张测试图片",
        concept="重新组织主体",
        retain=["主体"],
        transform=["材质"],
        discard=["背景"],
        title=title,
        production_prompt="测试提示词",
    )


def test_user_no_text_and_exact_title_override_package_defaults():
    draft = plan()
    assert "禁止" in title_policy(
        draft, PackageOptions(title_mode="exact", title="指定标题"), "不要文字"
    )
    assert draft.title == ""
    draft = plan()
    title_policy(draft, PackageOptions(title_mode="exact", title="山间日记"), "")
    assert draft.title == "山间日记"
    with pytest.raises(PackageExecutionError):
        title_policy(plan("one"), PackageOptions(), "")


@pytest.mark.parametrize("user_text", ["不要任何文字", "不要任何标题", "不添加文字", "NO TEXT"])
def test_explicit_no_text_overrides_model_none_literal(user_text):
    draft = plan("none")
    assert "禁止任何文字" in title_policy(draft, PackageOptions(), user_text)
    assert draft.title == ""


@pytest.mark.asyncio
async def test_no_text_request_is_resolved_before_visual_planning(monkeypatch):
    import json
    from app.services import package_execution as execution

    monkeypatch.setattr(execution, "_is_mock", lambda: False)

    async def visual(prompt, images, schema):
        payload = json.loads(prompt.split("\n", 1)[1])
        assert payload["title_policy"] == {"title_mode": "none", "title": ""}
        return plan("")

    monkeypatch.setattr(execution, "visual_json", visual)
    options = PackageOptions(title_mode="exact", title="旧标题")
    await execution.plan_images("style", b"source", [], options, "不要任何文字")
    assert options.title == "旧标题"  # Caller-owned confirmation input is unchanged.


@pytest.mark.asyncio
async def test_incompatible_model_fails_before_loading_or_planning():
    with pytest.raises(PackageExecutionError, match="多图"):
        await build_snapshot(
            None, None, None, "", "wanx2.1-imageedit", PackageOptions()
        )


@pytest.mark.asyncio
async def test_mock_output_does_not_claim_visual_success():
    image = png()
    result = await review_output({"size": "1536x1024"}, [image], image, simulated=True)
    assert result["status"] == "needs_review"
    assert not result["dimensions_match"] and result["visual_status"] == "not_evaluated"
    with pytest.raises(PackageExecutionError):
        normalize_image(b"not an image")


@pytest.mark.asyncio
async def test_adapter_sends_frozen_images_in_order_without_truncating_prompt(
    monkeypatch,
):
    from app.services import image_gen

    original_client = httpx.AsyncClient
    source, style = png(), png(color="blue")
    prompt = "保持指定构图。" * 400
    requests = []

    async def handler(request):
        requests.append(request)
        body = await request.aread()
        assert body.index(source) < body.index(style)
        assert prompt.encode() in body and b"1536x1024" in body
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(source).decode()}],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "private": "must drop",
                },
            },
        )

    monkeypatch.setattr(
        image_gen,
        "settings",
        SimpleNamespace(
            openai_api_key="test-only",
            package_generation_estimated_cost_yuan=0.42,
            openai_base_url="https://image-provider.example/v1/",
        ),
    )
    monkeypatch.setattr(
        image_gen.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    result = await image_gen.generate(
        "", prompt, model="gpt-image-2", image_inputs=[source, style], size="1536x1024"
    )
    assert result.cost_yuan == 0.42
    assert result.usage == {"input_tokens": 20, "output_tokens": 10}
    assert result.image_bytes == source and len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://image-provider.example/v1/images/edits"


@pytest.mark.asyncio
async def test_visual_review_uses_final_empty_title_not_concept(monkeypatch):
    from app.services import package_execution as execution
    from app.schemas.creative_plan import VisualReview

    monkeypatch.setattr(execution, "_is_mock", lambda: False)
    async def visual(prompt, images, schema):
        assert "最终文字要求：禁止任何文字" in prompt
        assert "概念名不是必须显示的标题" in prompt
        return VisualReview(subject_preserved=True, style_aligned=True,
                            title_correct=True, no_reference_copy=True, issues=[])
    monkeypatch.setattr(execution, "visual_json", visual)
    result = await execution.review_output(
        {"size": "1024x1024", "plan": plan("").model_dump()},
        [png()], png((1254, 1254)), simulated=False,
    )
    assert result["visual_status"] == "evaluated"
    assert result["visual"]["title_correct"] is True
    assert result["status"] == "needs_review"
    assert result["dimensions_match"] is False
