from scripts.calibrate_retrieval_threshold import calibrate, score_threshold


def _diagnostics(scores: dict[str, float]) -> dict:
    return {
        query_id: {"items": [{"score_semantic": score}]}
        for query_id, score in scores.items()
    }


def test_score_threshold_uses_strict_rejection_below_threshold() -> None:
    rows = [
        {"id": "p", "kind": "positive", "top_score": 0.8},
        {
            "id": "n",
            "kind": "negative",
            "negative_type": "absent",
            "top_score": 0.7,
        },
    ]

    result = score_threshold(rows, 0.8)

    assert result["positive_acceptance"] == 1.0
    assert result["negative_rejection"] == 1.0


def test_calibration_does_not_use_frozen_splits_for_selection() -> None:
    positive_queries = [
        {
            "id": "p-dev",
            "query": "dev positive",
            "split": "development",
            "relevant_photo_ids": ["photo"],
        },
        {
            "id": "p-val",
            "query": "val positive",
            "split": "validation",
            "relevant_photo_ids": ["photo"],
        },
    ]
    negative_queries = [
        {
            "id": "n-dev",
            "query": "dev negative",
            "split": "development",
            "must_return_empty": True,
            "negative_type": "absent",
        }
    ]

    report = calibrate(
        positive_queries,
        _diagnostics({"p-dev": 0.9, "p-val": 0.1}),
        negative_queries,
        _diagnostics({"n-dev": 0.2}),
        min_positive_acceptance=1.0,
    )

    assert 0.2 < report["constrained_selection"]["threshold"] < 0.9
    assert report["constrained_selection"]["positive_acceptance"] == 1.0
    assert report["frozen_evaluation"]["validation"]["positive_acceptance"] == 0.0
    assert report["single_threshold_suitable"] is True
