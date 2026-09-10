"""One scoped, durable undo record; never rewind a later search or generation."""

from copy import deepcopy
from uuid import uuid4


def capture_feedback(state, batch):
    visible = set(state.active_search.get("shown_photo_ids", []))
    visible.update(p["id"] for p in state.last_search_items)
    return {
        "undo_id": uuid4().hex,
        "goal_id": state.search_feedback.get("goal_id"),
        "batch_id": batch["batch_id"],
        "items": [
            deepcopy(p)
            for p in batch.get("items", state.last_search_items)
            if p["id"] in visible
        ],
        "last_search_items": deepcopy(state.last_search_items),
        "candidate_pool_items": deepcopy(
            state.active_search.get("candidate_pool_items", [])
        ),
        "selected": state.confirmed_photo_id,
        "feedback": deepcopy(state.search_feedback),
    }


async def undo_feedback(agent, user_id, state, undo_id):
    from app.services.agent_runtime import _refresh_candidate
    from app.services.agent_workflow import transition_workflow

    record = state.feedback_undo
    if (
        not record
        or record["undo_id"] != undo_id
        or record["goal_id"] != state.search_feedback.get("goal_id")
    ):
        return {
            "ok": False,
            "error_type": "stale_undo",
            "hint": "这次撤销已失效，请根据当前照片继续操作。",
        }
    removed = set(record["removed"])
    restored = []
    for item in record["items"]:
        if item["id"] in removed:
            fresh = await _refresh_candidate(agent.db, user_id, item, [])
            if fresh is None:
                return {
                    "ok": False,
                    "error_type": "unavailable_photo",
                    "hint": "照片已不可用，无法恢复这次反馈。",
                }
            restored.append(fresh)
    check = agent.db.info.get("ownership_check")
    if check:
        await check()
    by_id = {p["id"]: p for p in restored}
    state.rejected_photo_ids.difference_update(removed)
    state.last_search_items = [
        by_id.get(p["id"], p) for p in record["last_search_items"]
    ]
    state.active_search["candidate_pool_items"] = record["candidate_pool_items"]
    state.active_search["candidate_pool_count"] = len(record["candidate_pool_items"])
    state.active_search["rejected_photo_ids"] = sorted(state.rejected_photo_ids)
    state.search_feedback = record["feedback"]
    state.confirmed_photo_id = record["selected"]
    if state.confirmed_photo_id:
        transition_workflow(state, "selection_confirmed")
    state.feedback_undo = None
    payload = {
        "items": restored,
        "selected_photo_id": state.confirmed_photo_id,
        "undo_id": undo_id,
    }
    if state.emit_event:
        state.emit_event("feedback_undone", payload)
    return {"ok": True, "finish_turn": True, "message": "已撤销移除。", **payload}
