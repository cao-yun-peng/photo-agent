"""照片语义集合字段的统一写入与查询意图映射。"""

from __future__ import annotations

from typing import Any

from app.schemas.analysis import ImageAnalysis, infer_semantic_facets


def apply_semantic_facets(photo: Any, analysis: ImageAnalysis | dict[str, Any]) -> None:
    """把 JSON 分析结果同步到可索引的 Photo 列。"""

    payload = (
        analysis.model_dump(exclude_none=True)
        if isinstance(analysis, ImageAnalysis)
        else dict(analysis)
    )
    facets = infer_semantic_facets(payload)
    photo.photo_type = facets["photo_type"]
    photo.is_selfie = facets["is_selfie"]
    photo.people_count = facets["people_count"]


def clear_semantic_facets(photo: Any) -> None:
    photo.photo_type = None
    photo.is_selfie = None
    photo.people_count = None
