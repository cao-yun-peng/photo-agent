"""Explicit button actions share the tool executor, ownership lease and session lock."""

import json


async def run_ui_action(agent, user_id, payload, state, queue=None):
    from app.services.agent_runtime import _initialize_state, _remember_message

    state = await _initialize_state(agent, user_id, payload.query, None, state)
    action = payload.ui_action
    events = []

    def emit(kind, value):
        event = {"type": kind, "payload": value, "step": state.step}
        events.append(event)
        if queue is not None:
            queue.put_nowait(event)

    if action.action == "reject_photo":
        tool = "feedback_results"
        args = {
            "kind": "reject_items",
            "photo_ids": [str(action.photo_id)],
            "batch_id": action.batch_id,
        }
    elif action.action == "undo_feedback":
        tool, args = "undo_feedback", {"undo_id": action.undo_id}
    else:
        tool, args = "continue_search", {}
    state.emit_event = emit
    try:
        emit("start", {"session_id": str(state.session_id)})
        emit("tool_call", {"tool": tool, "arguments": json.dumps(args)})
        result = await agent._execute_tool(user_id, tool, json.dumps(args), state)
        emit("tool_result", {"tool": tool, "result": result})
        message = result.get("message") or result.get("hint") or "当前结果已更新。"
        emit("final", {"message": message})
        _remember_message(state, "user", payload.query)
        _remember_message(state, "assistant", message)
    finally:
        state.emit_event = None
    return state, events
