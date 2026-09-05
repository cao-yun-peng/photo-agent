from app.services.turn_resolver import resolve_turn_by_rule


def _items() -> list[dict]:
    return [
        {"id": "p-001", "ai_description": "窗台上的猫"},
        {"id": "p-002", "ai_description": "草地上的猫"},
    ]


def test_indexed_negative_feedback_targets_only_named_result() -> None:
    plan = resolve_turn_by_rule(
        "第2张不需要",
        active_search={"resolved_query": "猫"},
        last_search_items=_items(),
    )

    assert plan is not None
    assert plan.intent == "result_feedback"
    assert plan.feedback is not None
    assert plan.feedback.photo_ids == ["p-002"]
    assert plan.feedback.continue_search is False


def test_ambiguous_negative_feedback_asks_for_result_position() -> None:
    plan = resolve_turn_by_rule(
        "你给我的结果里有我不需要的",
        active_search={"resolved_query": "猫"},
        last_search_items=_items(),
    )

    assert plan is not None
    assert plan.intent == "result_feedback"
    assert plan.needs_clarification is True
    assert plan.clarification_options == ["第 1 张不需要", "第 2 张不需要"]


def test_single_rejected_result_can_continue_search() -> None:
    plan = resolve_turn_by_rule(
        "不要这张，有没有别的猫的照片",
        active_search={"resolved_query": "猫"},
        last_search_items=[_items()[0]],
    )

    assert plan is not None
    assert plan.intent == "result_feedback"
    assert plan.relation == "continue"
    assert plan.feedback is not None
    assert plan.feedback.photo_ids == ["p-001"]
    assert plan.feedback.continue_search is True


def test_feedback_can_recover_followup_query_from_legacy_state() -> None:
    plan = resolve_turn_by_rule(
        "不要这张，有没有别的猫的照片",
        active_search={},
        last_search_items=[_items()[0]],
    )

    assert plan is not None
    assert plan.feedback is not None
    assert plan.feedback.continue_search is True
    assert plan.feedback.search_query == "猫的照片"


def test_new_subject_search_is_not_misclassified_as_result_feedback() -> None:
    plan = resolve_turn_by_rule(
        "不要猫的了，帮我找狗的照片",
        active_search={"resolved_query": "猫"},
        last_search_items=[_items()[0]],
    )

    assert plan is not None
    assert plan.intent == "photo_search"
    assert plan.relation == "replace"
    assert plan.search is not None
    assert "狗" in plan.search.query


def test_lost_photo_reference_clarifies_instead_of_searching() -> None:
    plan = resolve_turn_by_rule(
        "继续上次，帮我改造那张照片",
        active_search={},
        last_search_items=[],
        confirmed_photo_id=None,
    )

    assert plan is not None
    assert plan.needs_clarification is True
    assert "哪张照片" in plan.clarification_question
