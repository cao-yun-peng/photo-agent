"""质量自检：输入预检 + 输出质量关卡。

原则：
- 输入预检纯规则 + 统计指标，零 token 开销；
- 输出质量关卡基于已有 AI 产物做快速校验，不调用 LLM"反思"；
- 所有失败都给出 reason 码，便于分级存储和离线统计。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image, UnidentifiedImageError

from app.schemas.analysis import ImageAnalysis

logger = logging.getLogger(__name__)

# 输入预检阈值
_MIN_PIXELS = 64 * 64          # 过小的图无法做有效 VL 分析
_MAX_PIXELS = 40_000_000       # 4000 万像素，超过可能 OOM 或超时
_MIN_UNIQUE_COLORS = 16        # 纯色/渐变极少色的图
_SOLID_COLOR_RATIO = 0.98      # 单一颜色占比超过 98% 视为纯色

# 输出质量阈值
_MIN_DESCRIPTION_LEN = 10      # ai_description 至少 10 个字符
_MAX_EMBEDDING_NORM = 10.0     # 超过该值视为异常向量


@dataclass
class PreflightResult:
    """输入预检结果。"""

    ok: bool
    reason: str | None = None
    width: int | None = None
    height: int | None = None
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class QualityGateResult:
    """输出质量关卡结果。"""

    ok: bool
    reason: str | None = None
    issues: list[str] = field(default_factory=list)
    storage_tier: str = "full"  # full | partial | skip


# ------------------------------------------------------------------
# 输入预检
# ------------------------------------------------------------------
def preflight_check(image_bytes: bytes | None) -> PreflightResult:
    """处理前检查图片是否可用。

    覆盖：
    - 空文件 / 无法识别格式
    - 文件损坏（PIL 加载失败）
    - 尺寸过小或过大
    - 空白 / 纯色 / 接近纯色
    """
    if image_bytes is None or len(image_bytes) == 0:
        return PreflightResult(ok=False, reason="empty_file")

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except UnidentifiedImageError:
        return PreflightResult(ok=False, reason="unsupported_format")
    except Exception as exc:  # noqa: BLE001
        logger.info("preflight_check image load failed | exc=%s", exc)
        return PreflightResult(ok=False, reason="corrupted_file")

    width, height = img.size
    pixel_count = width * height

    if pixel_count < _MIN_PIXELS:
        return PreflightResult(
            ok=False,
            reason="too_small",
            width=width,
            height=height,
            info={"min_pixels": _MIN_PIXELS},
        )
    if pixel_count > _MAX_PIXELS:
        return PreflightResult(
            ok=False,
            reason="too_large",
            width=width,
            height=height,
            info={"max_pixels": _MAX_PIXELS},
        )

    # 空白/纯色检测：采样统计
    try:
        # 统一转 RGB 后下采样，加速统计
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        small = rgb.resize((min(128, width), min(128, height)))
        arr = np.array(small)
        pixels = arr.reshape(-1, 3)

        unique_colors = len(np.unique(pixels, axis=0))
        total_pixels = pixels.shape[0]
        unique_ratio = unique_colors / total_pixels if total_pixels > 0 else 0

        # 最常见的颜色占比
        unique, counts = np.unique(pixels, axis=0, return_counts=True)
        most_common_ratio = counts.max() / total_pixels if total_pixels > 0 else 0

        info = {
            "unique_colors": int(unique_colors),
            "unique_ratio": round(unique_ratio, 4),
            "most_common_ratio": round(most_common_ratio, 4),
        }

        if unique_colors < _MIN_UNIQUE_COLORS or most_common_ratio > _SOLID_COLOR_RATIO:
            return PreflightResult(
                ok=False,
                reason="solid_or_blank",
                width=width,
                height=height,
                info=info,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("preflight_check color stats failed | exc=%s", exc)
        # 统计失败不阻断，继续处理

    return PreflightResult(ok=True, width=width, height=height)


# ------------------------------------------------------------------
# 输出质量关卡
# ------------------------------------------------------------------
def quality_gate(
    *,
    description: str | None,
    embedding: list[float] | None,
    analysis: ImageAnalysis | None,
) -> QualityGateResult:
    """AI 产物质量校验。

    覆盖：
    - 描述为空或过短
    - embedding 维度异常或数值异常
    - 结构化分析 summary 为空
    - 解析质量为 fallback / empty
    """
    issues: list[str] = []

    # 1. 描述检查
    if not description or len(description.strip()) < _MIN_DESCRIPTION_LEN:
        issues.append("description_too_short")

    # 2. embedding 检查。缺失表示服务降级，可保留其他 AI 产物；畸形向量不可安全索引。
    if embedding is None:
        issues.append("embedding_missing")
    else:
        try:
            vec = np.asarray(embedding, dtype=float)
        except (TypeError, ValueError):
            issues.append("embedding_invalid_numeric")
        else:
            if vec.ndim != 1 or vec.shape[0] != 1024:
                issues.append(f"embedding_dim_mismatch:{'x'.join(map(str, vec.shape))}")
            elif not np.isfinite(vec).all():
                issues.append("embedding_non_finite")
            else:
                norm = float(np.linalg.norm(vec))
                if norm == 0 or norm > _MAX_EMBEDDING_NORM:
                    issues.append(f"embedding_norm_abnormal:{norm:.2f}")

    # 3. 结构化分析检查
    if analysis is None:
        issues.append("analysis_missing")
    else:
        if not analysis.summary or len(analysis.summary.strip()) < 5:
            issues.append("analysis_summary_empty")
        if analysis.parse_quality != "ok":
            issues.append(f"analysis_parse_quality:{analysis.parse_quality}")

    # 单一产品语义：
    # - embedding 缺失是外部服务降级，保留描述/VL 分析并标记 partial_done；
    # - embedding 存在但畸形，说明产物不可安全索引，进入 skip，避免污染向量列。
    critical_reasons = {
        "embedding_dim_mismatch",
        "embedding_invalid_numeric",
        "embedding_non_finite",
        "embedding_norm_abnormal",
    }
    if any(issue.split(":")[0] in critical_reasons for issue in issues):
        return QualityGateResult(
            ok=False,
            reason="critical",
            issues=issues,
            storage_tier="skip",
        )

    if issues:
        return QualityGateResult(
            ok=False,
            reason="partial",
            issues=issues,
            storage_tier="partial",
        )

    return QualityGateResult(ok=True, storage_tier="full")


# ------------------------------------------------------------------
# 辅助：从 issues 生成原因码
# ------------------------------------------------------------------
def summarize_quality_reason(issues: list[str]) -> str:
    """把 issues 列表转成 photo.partial_reason 用的短码。"""
    if not issues:
        return ""
    first = issues[0]
    return first.split(":")[0]


# ------------------------------------------------------------------
# 分级存储决策 + 状态机
# ------------------------------------------------------------------
VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing", "failed"},
    "processing": {"done", "partial_done", "skipped", "failed"},
    "done": set(),
    "partial_done": set(),
    "skipped": set(),
    "failed": {"processing"},  # 允许重试
}


def can_transition(current: str, target: str) -> bool:
    """判断状态转换是否合法。"""
    return target in VALID_TRANSITIONS.get(current, set())


@dataclass
class StorageDecision:
    """分级存储决策结果。"""

    status: str
    partial_reason: str | None
    store_description: bool
    store_embedding: bool
    store_analysis: bool


def decide_storage(gate: QualityGateResult) -> StorageDecision:
    """根据质量关卡结果决定存储策略和最终状态。

    - full：全部存储，状态 done
    - partial：保存可用产物并标记 partial_done；缺失/降级产物不写入
    - skip：只保留缩略图和基础元数据，状态 skipped，不进入搜索索引
    """
    if gate.storage_tier == "full":
        return StorageDecision(
            status="done",
            partial_reason=None,
            store_description=True,
            store_embedding=True,
            store_analysis=True,
        )

    if gate.storage_tier == "partial":
        issue_codes = {issue.split(":")[0] for issue in gate.issues}
        return StorageDecision(
            status="partial_done",
            partial_reason=summarize_quality_reason(gate.issues),
            store_description=True,
            store_embedding="embedding_missing" not in issue_codes,
            store_analysis=True,
        )

    # skip
    return StorageDecision(
        status="skipped",
        partial_reason=summarize_quality_reason(gate.issues),
        store_description=False,
        store_embedding=False,
        store_analysis=False,
    )
