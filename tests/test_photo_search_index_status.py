from app.models.photo import Photo


def test_partial_photo_with_embedding_is_searchable() -> None:
    photo = Photo(
        status="partial_done",
        partial_reason="analysis_parse_quality",
        embedding=[0.0] * 1024,
    )

    assert photo.search_index_status == "ready"
    assert photo.search_index_message == "智能搜索已就绪"


def test_partial_photo_without_embedding_is_not_searchable() -> None:
    photo = Photo(
        status="partial_done",
        partial_reason="analysis_parse_quality",
        embedding=None,
    )

    assert photo.search_index_status == "unavailable"
