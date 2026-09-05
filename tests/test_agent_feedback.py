from uuid import uuid4

from app.services.agent_runtime import _apply_result_feedback_to_state
from app.services.agent_state import AgentState


def test_apply_result_feedback_updates_all_current_search_boundaries() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="第2张不需要",
        confirmed_photo_id="p-002",
        last_search_items=[{"id": "p-001"}, {"id": "p-002"}],
        active_search={
            "resolved_query": "猫",
            "shown_photo_ids": ["p-001", "p-002"],
            "candidate_pool_items": [{"id": "p-002"}, {"id": "p-003"}],
        },
    )

    applied = _apply_result_feedback_to_state(state, ["p-002", "not-current"])

    assert applied == ["p-002"]
    assert state.rejected_photo_ids == {"p-002"}
    assert state.confirmed_photo_id is None
    assert state.last_search_items == [{"id": "p-001"}]
    assert state.active_search["candidate_pool_items"] == [{"id": "p-003"}]
    assert state.active_search["candidate_pool_count"] == 1
    assert state.active_search["rejected_photo_ids"] == ["p-002"]

