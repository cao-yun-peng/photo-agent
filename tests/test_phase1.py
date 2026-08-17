"""Phase 1 核心逻辑单元测试（无需数据库/外部服务）."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.schemas.analysis import ImageAnalysis
from app.services.agent import PhotoAgent
from app.services.ai import _stable_mock_seed, build_retrieval_text, parse_vl_response
from app.services.quality import (
    QualityGateResult,
    can_transition,
    decide_storage,
    preflight_check,
    quality_gate,
)


# ------------------------------------------------------------------
# VL 结构化分析解析
# ------------------------------------------------------------------
def test_mock_embedding_seed_is_stable() -> None:
    assert _stable_mock_seed("去年在西湖拍的照片") == 13096972177322108030
    assert _stable_mock_seed("去年在西湖拍的照片") != _stable_mock_seed("海边的猫")


def test_parse_vl_response_ok() -> None:
    raw = """{"scene": "户外", "scene_detail": "海边", "persons": {"count": 2},
    "objects": ["沙滩", "海浪"], "text_in_image": [], "mood": "轻松",
    "colors": ["蓝色"], "summary": "海边沙滩上有两个人"}"""
    analysis = parse_vl_response(raw)
    assert analysis.scene == "户外"
    assert analysis.persons.count == 2
    assert "沙滩" in analysis.objects
    assert analysis.parse_quality == "ok"


def test_parse_vl_response_with_markdown() -> None:
    raw = "```json\n{\"scene\": \"室内\", \"summary\": \"室内照片\"}\n```"
    analysis = parse_vl_response(raw)
    assert analysis.scene == "室内"
    assert analysis.summary == "室内照片"


def test_parse_vl_response_fallback() -> None:
    raw = "这张图里有个人在户外餐厅吃饭，氛围很温馨。"
    analysis = parse_vl_response(raw)
    assert analysis.parse_quality == "fallback"
    assert "户外" in analysis.scene or "餐厅" in analysis.scene
    assert analysis.summary != ""


def test_parse_vl_response_empty() -> None:
    analysis = parse_vl_response("")
    assert analysis.parse_quality != "ok"


def test_build_retrieval_text_includes_fine_grained_visual_fields() -> None:
    analysis = ImageAnalysis(
        scene="车内",
        actions=["孩子向右跑动"],
        age_groups=["儿童"],
        blur_type="运动模糊",
        capture_context=["公交车内", "隔窗拍摄"],
        spatial_layout=["孩子位于画面右侧"],
        summary="公交车内拍摄的孩子跑动画面",
    )
    text = build_retrieval_text("一张车内照片", analysis)
    assert "动作：孩子向右跑动" in text
    assert "模糊类型：运动模糊" in text
    assert "拍摄方式：公交车内、隔窗拍摄" in text
    assert "空间位置：孩子位于画面右侧" in text


# ------------------------------------------------------------------
# 输入预检
# ------------------------------------------------------------------
def _make_image(
    width: int, height: int, color: tuple[int, int, int] | None = None
) -> bytes:
    if color is not None:
        img = Image.new("RGB", (width, height), color)
    else:
        # 生成有渐变/噪点的图，避免被判定为纯色
        img = Image.linear_gradient("L").resize((width, height)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_preflight_check_ok() -> None:
    raw = _make_image(800, 600)
    result = preflight_check(raw)
    assert result.ok is True
    assert result.width == 800


def test_preflight_check_too_small() -> None:
    raw = _make_image(50, 50)
    result = preflight_check(raw)
    assert result.ok is False
    assert result.reason == "too_small"


def test_preflight_check_solid_color() -> None:
    raw = _make_image(200, 200, (255, 255, 255))
    result = preflight_check(raw)
    assert result.ok is False
    assert result.reason == "solid_or_blank"


def test_preflight_check_empty() -> None:
    result = preflight_check(b"")
    assert result.ok is False
    assert result.reason == "empty_file"


# ------------------------------------------------------------------
# 输出质量关卡
# ------------------------------------------------------------------
def test_quality_gate_full() -> None:
    analysis = ImageAnalysis(
        scene="户外",
        summary="一张海边的照片",
        parse_quality="ok",
    )
    embedding = [0.01] * 1024
    gate = quality_gate(
        description="一张海边的照片，有两个人",
        embedding=embedding,
        analysis=analysis,
    )
    assert gate.ok is True
    assert gate.storage_tier == "full"


def test_quality_gate_partial_description_too_short() -> None:
    analysis = ImageAnalysis(summary="海边", parse_quality="ok")
    gate = quality_gate(
        description="海边",
        embedding=[0.01] * 1024,
        analysis=analysis,
    )
    assert gate.ok is False
    assert gate.storage_tier == "partial"
    assert "description_too_short" in gate.issues


def test_quality_gate_partial_missing_embedding() -> None:
    analysis = ImageAnalysis(summary="海边", parse_quality="ok")
    gate = quality_gate(
        description="一张海边的照片",
        embedding=None,
        analysis=analysis,
    )
    assert gate.ok is False
    assert gate.storage_tier == "partial"
    decision = decide_storage(gate)
    assert decision.status == "partial_done"
    assert decision.store_description is True
    assert decision.store_analysis is True
    assert decision.store_embedding is False


@pytest.mark.parametrize(
    ("embedding", "expected_issue"),
    [
        ([0.01] * 12, "embedding_dim_mismatch"),
        ([float("nan")] * 1024, "embedding_non_finite"),
        ([0.0] * 1024, "embedding_norm_abnormal"),
        (["not-a-number"] * 1024, "embedding_invalid_numeric"),
    ],
)
def test_quality_gate_skips_unsafe_embedding(
    embedding: list[float] | list[str], expected_issue: str
) -> None:
    analysis = ImageAnalysis(summary="海边照片", parse_quality="ok")
    gate = quality_gate(
        description="一张清晰的海边照片",
        embedding=embedding,  # type: ignore[arg-type]
        analysis=analysis,
    )
    assert gate.storage_tier == "skip"
    assert any(issue.startswith(expected_issue) for issue in gate.issues)


# ------------------------------------------------------------------
# 状态机 + 分级存储
# ------------------------------------------------------------------
def test_can_transition() -> None:
    assert can_transition("pending", "processing") is True
    assert can_transition("processing", "done") is True
    assert can_transition("done", "processing") is False
    assert can_transition("failed", "processing") is True


def test_decide_storage_full() -> None:
    gate = QualityGateResult(ok=True, storage_tier="full")
    decision = decide_storage(gate)
    assert decision.status == "done"
    assert decision.store_embedding is True


def test_decide_storage_skip() -> None:
    gate = QualityGateResult(
        ok=False, storage_tier="skip", issues=["embedding_dim_mismatch:12"]
    )
    decision = decide_storage(gate)
    assert decision.status == "skipped"
    assert decision.store_embedding is False


# ------------------------------------------------------------------
# Agent 核心（mock 模式，无需 LLM）
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_run_mock() -> None:
    # 使用一个伪造的 db 对象，mock 模式下不会真正查询数据库
    class FakeDB:
        pass

    agent = PhotoAgent(db=FakeDB())  # type: ignore[arg-type]
    state, events = await agent.run(
        user_id="12345678-1234-5678-1234-567812345678",  # type: ignore[arg-type]
        query="找一张去年海边的照片",
    )

    event_types = [e["type"] for e in events]
    assert "start" in event_types
    assert "think" in event_types
    assert "final" in event_types
    assert state.step >= 1
