"""第三阶段：集合语义、阈值和覆盖率的回归测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.analysis import ImageAnalysis
from app.schemas.photo import SearchQuery
from app.services.search import (
    apply_semantic_threshold,
    build_search_coverage_hint,
    infer_complete_result_filters,
    resolve_semantic_threshold,
)
from app.services.search_index import get_index_coverage
from app.services.semantic_facets import apply_semantic_facets


def test_v4_selfie_is_normalized_to_v5_facets_without_losing_person_count() -> None:
    analysis = ImageAnalysis.model_validate(
        {
            "persons": {"count": 1},
            "capture_context": ["镜面自拍"],
            "summary": "女生在镜子前自拍",
            "analysis_version": "v4",
        }
    )

    assert analysis.photo_type == "selfie"
    assert analysis.is_selfie is True
    assert analysis.people_count == 1
    assert analysis.persons.count == 1


def test_group_photo_facets_are_written_to_denormalized_columns() -> None:
    photo = SimpleNamespace(photo_type=None, is_selfie=None, people_count=None)
    analysis = ImageAnalysis(
        persons={"count": 4},
        photo_type="group_photo",
        people_count=4,
        summary="四人合影",
    )

    apply_semantic_facets(photo, analysis)

    assert photo.photo_type == "group_photo"
    assert photo.is_selfie is False
    assert photo.people_count == 4


def test_collection_intents_cover_selfie_screenshot_group_and_negative_selfie() -> None:
    assert infer_complete_result_filters("全部截图")["photo_types"] == ["screenshot"]
    group = infer_complete_result_filters("把所有合影给我")
    assert group["photo_types"] == ["group_photo"]
    assert group["people_count_min"] == 2
    negative = infer_complete_result_filters("不是自拍的单人照")
    assert negative["is_selfie"] is False
    assert negative["photo_types"] == ["portrait"]


def test_structured_collection_bypasses_similarity_threshold() -> None:
    assert resolve_semantic_threshold(0.85, structured_collection=True) == (
        None,
        "structured_collection_filter",
    )


def test_semantic_threshold_filters_only_below_cutoff() -> None:
    scored = [
        ("a", 0.91, 0.0, 0.0, 0.91),
        ("b", 0.84, 0.0, 0.0, 0.84),
        ("c", 0.72, 0.0, 0.0, 0.72),
    ]
    kept, filtered = apply_semantic_threshold(scored, 0.8)
    assert [item[0] for item in kept] == ["a", "b"]
    assert filtered == 1


def test_search_query_rejects_inverted_people_range() -> None:
    with pytest.raises(ValidationError, match="people_count_min"):
        SearchQuery(q="合照", people_count_min=3, people_count_max=1)


@pytest.mark.asyncio
async def test_index_coverage_separates_embedding_and_v5_facets() -> None:
    result = MagicMock()
    result.one.return_value = (10, 10, 0, 7)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    coverage = await get_index_coverage(db, uuid4())

    assert coverage["complete"] is True
    assert coverage["semantic_complete"] is False
    assert coverage["facet_coverage_ratio"] == 0.7
    assert "3 张" in coverage["semantic_message"]


def test_coverage_hint_reports_reindex_and_threshold() -> None:
    hint = build_search_coverage_hint(
        {
            "complete": True,
            "semantic_complete": False,
            "semantic_message": "3 张尚未完成 v5 语义重索引",
        },
        requires_facets=True,
        threshold=0.8,
        threshold_filtered_count=2,
    )
    assert hint is not None
    assert "v5 语义重索引" in hint
    assert "0.80" in hint
