import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.agent import PhotoAgent
from app.services.agent_registry import _build_registry
from app.services.agent_state import AgentState


@pytest.fixture
def harness(monkeypatch):
    from app.services import agent_execution, agent as module

    registry = _build_registry()
    outputs = []
    calls = []

    async def search(**args):
        calls.append({k: v for k, v in args.items() if k not in {"db", "user_id"}})
        return deepcopy(outputs.pop(0))

    async def maintain(agent, deps, uid, name, args, result, state):
        if result.get("_search_plan_id"):
            state.active_search["plan_id"] = result.pop("_search_plan_id")
        return [], None, None

    for name in ("search_photos", "fallback_search", "browse_candidates"):
        registry.get(name).fn = search
    monkeypatch.setattr(agent_execution, "_run_search_maintenance", maintain)
    monkeypatch.setattr(
        "app.services.search_candidate_pool.begin_candidate_search",
        AsyncMock(return_value=None),
    )
    agent = PhotoAgent(SimpleNamespace(info={}), registry=registry)
    st = AgentState(uuid4(), uuid4(), "", agent_variant="v2")
    decisions = []
    llm = AsyncMock(side_effect=lambda *_: (decisions.pop(0), {"total_tokens": 1}))
    monkeypatch.setattr(module, "_llm_decide", llm)

    async def turn(name, args, result=None, query="测试输入"):
        if result is not None:
            outputs.append(result)
        decisions.append(
            {
                "tool_calls": [
                    {
                        "id": "one",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
            }
        )
        _, events = await agent.run(st.user_id, query, initial_state=st)
        return events

    return SimpleNamespace(
        agent=agent,
        state=st,
        turn=turn,
        outputs=outputs,
        calls=calls,
        decisions=decisions,
        llm=llm,
    )


def result(*ids, **extra):
    return {
        "ok": True,
        "items": [{"id": value} for value in ids],
        "_search_plan_id": "plan",
        "next_cursor": "cursor",
        **extra,
    }


@pytest.mark.asyncio
async def test_first_search_one_decision_and_switch_clears_selection(harness):
    h = harness
    events = await h.turn(
        "search_photos", {"query": "猫", "change": "new"}, result("a", "b")
    )
    assert h.llm.await_count == 1
    assert not any(e["type"] == "route" for e in events)
    assert any(e["type"] == "search_state" for e in events)
    old_goal = h.state.search_feedback["goal_id"]
    h.state.confirmed_photo_id = "a"
    h.state.rejected_photo_ids.add("b")
    await h.turn("search_photos", {"query": "狗", "change": "new"}, result("d"))
    assert h.state.search_feedback["goal_id"] != old_goal
    assert not h.state.confirmed_photo_id
    assert not h.state.rejected_photo_ids
    assert h.state.active_search["shown_photo_ids"] == ["d"]


@pytest.mark.asyncio
async def test_continue_inherits_filters_and_keeps_selection(harness):
    h = harness
    await h.turn(
        "search_photos",
        {
            "query": "北京黑猫",
            "change": "new",
            "from_date": "2025-01-01",
            "photo_types": ["portrait"],
            "limit": 3,
            "result_mode": "select",
        },
        result("a"),
    )
    h.state.confirmed_photo_id = "a"
    events = await h.turn("continue_search", {}, result("a", "b"), "还有吗")
    assert h.calls[-1]["query"] == "北京黑猫"
    assert h.calls[-1]["photo_types"] == ["portrait"]
    assert h.calls[-1]["cursor"] == "cursor"
    assert h.calls[-1]["limit"] == 3
    assert h.state.confirmed_photo_id == "a"
    assert h.state.active_search["shown_photo_ids"] == ["a", "b"]
    assert not h.state.rejected_photo_ids
    returned = next(
        e["payload"]["result"] for e in events if e["type"] == "tool_result"
    )
    assert returned["display_mode"] == "append"
    assert [p["id"] for p in returned["items"]] == ["b"]


@pytest.mark.asyncio
async def test_refine_restarts_cursor_preserves_rejections(harness):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a"))
    goal = h.state.search_feedback["goal_id"]
    h.state.rejected_photo_ids.add("a")
    await h.turn("search_photos", {"query": "黑猫", "change": "refine"}, result("b"))
    assert h.state.search_feedback["goal_id"] == goal
    assert h.calls[-1]["exclude_photo_ids"] == ["a"]
    assert "cursor" not in h.calls[-1]


@pytest.mark.asyncio
async def test_feedback_batches_upgrade_once_and_remove_selection(harness):
    h = harness
    await h.turn(
        "search_photos",
        {"query": "猫", "change": "new", "result_mode": "select"},
        result("a", "b"),
    )
    batch = h.state.search_feedback["batch_id"]
    h.state.confirmed_photo_id = "a"
    await h.turn("feedback_results", {"kind": "reject_batch", "batch_id": batch})
    await h.turn("continue_search", {}, result("c"))
    assert h.calls[-1]["feedback_level"] == 1
    assert not h.state.confirmed_photo_id
    assert h.state.rejected_photo_ids == {"a", "b"}
    count = len(h.calls)
    await h.turn("feedback_results", {"kind": "reject_batch", "batch_id": batch})
    assert len(h.calls) == count
    assert h.state.search_feedback["rejected_batch_count"] == 1
    await h.turn("feedback_results", {"kind": "reject_batch"})
    await h.turn("continue_search", {}, result("d", browse_scope="clues"))
    assert h.calls[-1]["feedback_level"] == 2
    assert h.state.search_feedback["scope"] == "clues"


@pytest.mark.asyncio
async def test_single_rejection_does_not_reject_batch(harness):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a", "b"))
    await h.turn("feedback_results", {"kind": "reject_items", "photo_ids": ["b"]})
    assert h.state.rejected_photo_ids == {"b"}
    assert h.state.search_feedback["rejected_batch_count"] == 0
    assert [p["id"] for p in h.state.last_search_items] == ["a"]


@pytest.mark.asyncio
async def test_failures_preserve_continuation_but_not_old_goal(harness):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a"))
    before = deepcopy(h.state.active_search)
    h.outputs.append({"ok": False, "error_type": "timeout"})
    response = await h.agent._execute_tool(
        h.state.user_id, "continue_search", "{}", h.state
    )
    assert response["ok"] is False
    assert h.state.active_search == before
    h.outputs.append({"ok": False, "error_type": "timeout"})
    await h.agent._execute_tool(
        h.state.user_id, "search_photos", '{"query":"狗","change":"new"}', h.state
    )
    assert h.state.active_search["resolved_query"] == "狗"
    assert h.state.last_search_items == []


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_mutate_state(harness):
    h = harness
    for name, args in [
        ("search_photos", {"query": "猫"}),
        ("continue_search", {"cursor": "invented"}),
        ("search_photos", {"query": "猫", "change": "new", "from_date": "bad"}),
    ]:
        before = h.state.to_json()
        response = await h.agent._execute_tool(
            h.state.user_id, name, json.dumps(args), h.state
        )
        assert not response["ok"]
        assert h.state.to_json() == before


@pytest.mark.asyncio
async def test_browse_then_continue(harness):
    h = harness
    await h.turn("browse_album", {}, result("a"))
    await h.turn("continue_search", {}, result("b", next_cursor=None))
    assert h.state.search_feedback["scope"] == "all"
    assert h.state.active_search["shown_photo_ids"] == ["a", "b"]
    calls = len(h.calls)
    await h.turn("continue_search", {})
    assert len(h.calls) == calls


@pytest.mark.asyncio
async def test_explicit_continue_loop(harness):
    h = harness
    h.decisions.append(
        {
            "tool_calls": [
                {
                    "id": "one",
                    "function": {
                        "name": "search_photos",
                        "arguments": '{"query":"猫","change":"new","finish_turn":false}',
                    },
                }
            ]
        }
    )
    h.decisions.append({"content": "已找到候选，接下来请选一张。"})
    h.outputs.append(result("a"))
    await h.agent.run(h.state.user_id, "找猫然后给建议", initial_state=h.state)
    assert h.llm.await_count == 2


def test_old_session_followup_is_not_persisted_as_action():
    st = AgentState(uuid4(), uuid4(), "猫", followup_type="more_search_results")
    restored = AgentState.from_json(st.to_json())
    assert restored.search_action is None
    assert restored.result_batches == []


@pytest.mark.asyncio
async def test_stale_batch_and_out_of_range_references_do_not_mutate(harness):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a"))
    old = h.state.search_feedback["batch_id"]
    await h.turn("search_photos", {"query": "狗", "change": "new"}, result("d"))
    for args in (
        {"kind": "reject_batch", "batch_id": old},
        {"kind": "reject_items", "photo_ids": ["invented"]},
    ):
        before = h.state.to_json()
        res = await h.agent._execute_tool(
            h.state.user_id, "feedback_results", json.dumps(args), h.state
        )
        assert not res["ok"]
        assert h.state.to_json() == before


@pytest.mark.asyncio
async def test_terminal_search_skips_later_generation_in_same_response(harness):
    h = harness
    h.decisions.append(
        {
            "tool_calls": [
                {
                    "id": "a",
                    "function": {
                        "name": "search_photos",
                        "arguments": '{"query":"猫","change":"new"}',
                    },
                },
                {"id": "b", "function": {"name": "apply_skill", "arguments": "{}"}},
            ]
        }
    )
    h.outputs.append(result("a"))
    _, events = await h.agent.run(
        h.state.user_id, "只找猫，不生成", initial_state=h.state
    )
    assert [e["payload"]["tool"] for e in events if e["type"] == "tool_call"] == [
        "search_photos"
    ]


@pytest.mark.asyncio
async def test_old_session_without_plan_requires_new_search(harness):
    h = harness
    h.state.active_search = {"resolved_query": "猫", "shown_photo_ids": ["a"]}
    res = await h.agent._execute_tool(h.state.user_id, "continue_search", "{}", h.state)
    assert res["error_type"] == "missing_search_plan"
    assert not h.calls


@pytest.mark.asyncio
async def test_ownership_check_happens_before_goal_reset(harness):
    h = harness
    h.agent.db.info["ownership_check"] = AsyncMock(side_effect=RuntimeError("lost"))
    before = h.state.to_json()
    with pytest.raises(RuntimeError, match="lost"):
        await h.agent._execute_tool(
            h.state.user_id, "search_photos", '{"query":"猫","change":"new"}', h.state
        )
    assert h.state.to_json() == before


@pytest.mark.asyncio
async def test_historical_feedback_after_refine_and_candidate_recheck(
    harness, monkeypatch
):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a"))
    old_batch = h.state.search_feedback["batch_id"]
    await h.turn("search_photos", {"query": "黑猫", "change": "refine"}, result("b"))
    await h.turn(
        "feedback_results",
        {"kind": "reject_items", "photo_ids": ["a"], "batch_id": old_batch},
    )
    assert "a" in h.state.rejected_photo_ids
    h.state.active_search["candidate_pool_items"] = [{"id": "deleted"}, {"id": "c"}]
    refresh = AsyncMock(side_effect=[None, {"id": "c"}])
    monkeypatch.setattr("app.services.agent_runtime._refresh_candidate", refresh)
    calls = len(h.calls)
    await h.turn("continue_search", {})
    assert len(h.calls) == calls
    assert [p["id"] for p in h.state.last_search_items] == ["c"]


@pytest.mark.asyncio
async def test_action_timeout_returns_tool_error(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(
        "app.services.agent_actions.execute_action",
        AsyncMock(side_effect=TimeoutError()),
    )
    res = await h.agent._execute_tool(h.state.user_id, "continue_search", "{}", h.state)
    assert res["error_type"] == "tool_timeout"


@pytest.mark.asyncio
async def test_waiting_for_selection_is_terminal_even_with_more_work(harness):
    h = harness
    await h.turn(
        "search_photos",
        {"query": "猫", "change": "new", "result_mode": "select", "finish_turn": False},
        result("a"),
    )
    assert h.llm.await_count == 1
    assert h.state.workflow_state == "awaiting_selection"


@pytest.mark.asyncio
async def test_text_clarification_preserves_results_and_history(harness):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a", "b"))
    before = deepcopy(h.state.last_search_items)
    h.decisions.append({"content": "你指哪一张？请告诉我序号。", "tool_calls": []})
    _, events = await h.agent.run(h.state.user_id, "这张不要", initial_state=h.state)
    assert h.state.last_search_items == before
    assert not h.state.rejected_photo_ids
    assert not any(e["type"] == "tool_call" for e in events)
    assert any(
        e["type"] == "final" and "哪一张" in e["payload"]["message"] for e in events
    )
    assert "哪一张" in str(h.state.recent_messages)
    await h.turn(
        "feedback_results",
        {"kind": "reject_items", "photo_ids": ["b"]},
        query="第二张",
    )
    assert h.state.rejected_photo_ids == {"b"}


@pytest.mark.asyncio
async def test_reject_then_continue_in_one_decision(harness):
    h = harness
    await h.turn(
        "search_photos",
        {"query": "猫", "change": "new", "result_mode": "select"},
        result("a", "b"),
    )
    before = h.llm.await_count
    h.outputs.append(result("c"))
    h.decisions.append(
        {
            "tool_calls": [
                {
                    "id": "reject",
                    "type": "function",
                    "function": {
                        "name": "feedback_results",
                        "arguments": json.dumps(
                            {
                                "kind": "reject_items",
                                "photo_ids": ["b"],
                                "finish_turn": False,
                            }
                        ),
                    },
                },
                {
                    "id": "more",
                    "type": "function",
                    "function": {"name": "continue_search", "arguments": "{}"},
                },
            ]
        }
    )
    await h.agent.run(h.state.user_id, "第二张不要，再找一些", initial_state=h.state)
    assert h.llm.await_count == before + 1
    assert h.state.rejected_photo_ids == {"b"}
    assert [p["id"] for p in h.state.last_search_items] == ["c"]


@pytest.mark.asyncio
async def test_undo_feedback_restores_photo_selection_and_json(harness, monkeypatch):
    from app.services.agent_state import AgentState

    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a", "b"))
    h.state.confirmed_photo_id = "b"
    await h.turn("feedback_results", {"kind": "reject_items", "photo_ids": ["b"]})
    token = h.state.feedback_undo["undo_id"]
    restored = AgentState.from_json(h.state.to_json())
    monkeypatch.setattr(
        "app.services.agent_runtime._refresh_candidate",
        AsyncMock(side_effect=lambda db, uid, item, excluded: item),
    )
    out = await h.agent._execute_tool(
        restored.user_id, "undo_feedback", json.dumps({"undo_id": token}), restored
    )
    assert out["ok"]
    assert not restored.rejected_photo_ids
    assert restored.confirmed_photo_id == "b"
    assert [p["id"] for p in restored.last_search_items] == ["a", "b"]
    assert [p["batch_position"] for p in restored.last_search_items] == [1, 2]
    duplicate = await h.agent._execute_tool(
        restored.user_id, "undo_feedback", json.dumps({"undo_id": token}), restored
    )
    assert duplicate["error_type"] == "stale_undo"


@pytest.mark.asyncio
async def test_undo_freshness_failure_leaves_rejection(harness, monkeypatch):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a", "b"))
    await h.turn("feedback_results", {"kind": "reject_items", "photo_ids": ["b"]})
    monkeypatch.setattr(
        "app.services.agent_runtime._refresh_candidate", AsyncMock(return_value=None)
    )
    out = await h.agent._execute_tool(
        h.state.user_id,
        "undo_feedback",
        json.dumps({"undo_id": h.state.feedback_undo["undo_id"]}),
        h.state,
    )
    assert out["error_type"] == "unavailable_photo"
    assert h.state.rejected_photo_ids == {"b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_tool,args",
    [
        ("continue_search", {}),
        ("search_photos", {"query": "狗", "change": "new"}),
        ("search_photos", {"query": "黑猫", "change": "refine"}),
    ],
)
async def test_search_invalidates_undo(harness, next_tool, args):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a", "b"))
    await h.turn("feedback_results", {"kind": "reject_items", "photo_ids": ["b"]})
    token = h.state.feedback_undo["undo_id"]
    await h.turn(next_tool, args, result("c"))
    out = await h.agent._execute_tool(
        h.state.user_id, "undo_feedback", json.dumps({"undo_id": token}), h.state
    )
    assert out["error_type"] == "stale_undo"


@pytest.mark.asyncio
async def test_ui_reject_uses_executor_without_model_and_rejects_forged_batch(harness):
    from app.schemas.agent import AgentRunRequest
    from app.services.agent_ui_actions import run_ui_action

    h = harness
    photo = str(uuid4())
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result(photo))
    batch = h.state.search_feedback["batch_id"]
    calls = h.llm.await_count
    payload = AgentRunRequest(
        query="不要这张",
        session_id=h.state.session_id,
        ui_action={"action": "reject_photo", "photo_id": photo, "batch_id": "0" * 32},
    )
    _, events = await run_ui_action(h.agent, h.state.user_id, payload, h.state)
    assert not h.state.rejected_photo_ids
    assert any(
        e["type"] == "tool_result" and not e["payload"]["result"]["ok"] for e in events
    )
    payload.ui_action.batch_id = batch
    _, events = await run_ui_action(h.agent, h.state.user_id, payload, h.state)
    assert h.llm.await_count == calls
    assert h.state.rejected_photo_ids == {photo}
    assert any(e["type"] == "feedback" and e["payload"]["undo_id"] for e in events)


@pytest.mark.asyncio
async def test_feedback_legacy_continuation_parameter_is_rejected(harness):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a"))
    out = await h.agent._execute_tool(
        h.state.user_id,
        "feedback_results",
        '{"kind":"reject_items","photo_ids":["a"],"continue_search":true}',
        h.state,
    )
    assert out["error_type"] == "invalid_arguments"
    assert not h.state.rejected_photo_ids


@pytest.mark.asyncio
async def test_undo_batch_feedback_restores_scope_strategy(harness, monkeypatch):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a", "b"))
    await h.turn("feedback_results", {"kind": "reject_batch"})
    assert len(h.calls) == 1
    assert h.state.search_feedback["pending_expansion"] == 1
    token = h.state.feedback_undo["undo_id"]
    monkeypatch.setattr(
        "app.services.agent_runtime._refresh_candidate",
        AsyncMock(side_effect=lambda db, uid, item, excluded: item),
    )
    out = await h.agent._execute_tool(
        h.state.user_id, "undo_feedback", json.dumps({"undo_id": token}), h.state
    )
    assert out["ok"]
    assert not h.state.search_feedback.get("pending_expansion")
    assert h.state.search_feedback["rejected_batch_count"] == 0
    assert not h.state.rejected_photo_ids


@pytest.mark.asyncio
async def test_undo_ownership_loss_prevents_mutation(harness, monkeypatch):
    h = harness
    await h.turn("search_photos", {"query": "猫", "change": "new"}, result("a"))
    await h.turn("feedback_results", {"kind": "reject_items", "photo_ids": ["a"]})
    monkeypatch.setattr(
        "app.services.agent_runtime._refresh_candidate",
        AsyncMock(side_effect=lambda db, uid, item, excluded: item),
    )
    h.agent.db.info["ownership_check"] = AsyncMock(
        side_effect=[None, RuntimeError("ownership lost")]
    )
    with pytest.raises(RuntimeError, match="ownership lost"):
        await h.agent._execute_tool(
            h.state.user_id,
            "undo_feedback",
            json.dumps({"undo_id": h.state.feedback_undo["undo_id"]}),
            h.state,
        )
    assert h.state.rejected_photo_ids == {"a"}


def test_ui_action_schema_rejects_ambiguous_payloads():
    from app.schemas.agent import AgentRunRequest
    from pydantic import ValidationError

    for action in [
        {"action": "reject_photo"},
        {"action": "continue_search", "undo_id": "a" * 32},
        {"action": "undo_feedback", "undo_id": "a" * 32, "extra": "x"},
    ]:
        with pytest.raises(ValidationError):
            AgentRunRequest(query="操作", ui_action=action)
