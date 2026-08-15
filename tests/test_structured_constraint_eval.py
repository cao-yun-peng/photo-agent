from scripts.evaluate_structured_constraints import replay


def test_replay_filters_mismatch_and_scores_negative_empty() -> None:
    queries = [
        {
            "id": "n1",
            "split": "development",
            "query": "写着HELLO的门垫",
            "relevant_photo_ids": [],
            "must_return_empty": True,
        }
    ]
    retrieval = {
        "results": {"n1": ["p-1"]},
        "diagnostics": {
            "n1": {"items": [{"photo_id": "p-1", "ai_description": "门垫"}]}
        },
    }
    predictions = {
        "p-1": {"objects": ["门垫"], "text_in_image": ["WELCOME"]}
    }

    report = replay(
        queries,
        retrieval,
        predictions,
        split="development",
        k=5,
    )

    assert report["constraint_summary"]["queries_with_constraints"] == 1
    assert report["details"][0]["filtered"] == []
    assert report["evaluation"]["summary"]["empty_query_accuracy"] == 1.0
