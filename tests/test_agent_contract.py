import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.registry import PromptRegistry
from app.services.agent import PhotoAgent
from app.services.agent_contract import capability_prompt, generation_message
from app.services.agent_runtime import _run_llm_loop
from app.services.agent_state import AgentState


def state(variant="v2", selected=None):
    return AgentState(
        uuid4(),
        uuid4(),
        "介绍一下修图功能",
        agent_variant=variant,
        confirmed_photo_id=selected,
    )


@pytest.mark.parametrize("variant", ["control", "v2"])
@pytest.mark.parametrize("selected", [None, "photo-1"])
def test_default_prompt_references_only_advertised_tools(variant, selected):
    agent = PhotoAgent(
        SimpleNamespace(info={}), system_prompt=PromptRegistry.DEFAULT_SYSTEM_PROMPT
    )
    schemas = agent._tool_schemas_for_state(state(variant, selected))
    names = {s["function"]["name"] for s in schemas}
    prompt = capability_prompt(agent.system_prompt, schemas)
    for name in agent.registry._tools:
        assert (name in prompt) == (name in names)
    assert "普通问答和澄清都直接用自然语言回复" in prompt
    assert "ask_clarification" not in names
    if variant == "v2":
        assert ("apply_skill" in names) == bool(selected)


def test_custom_prompt_retains_style_but_gets_runtime_contract():
    prompt = capability_prompt("回答尽量简短。", [])
    assert prompt.startswith("回答尽量简短。")
    assert "优先于前面的自定义说明" in prompt


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"status": "pending", "confirmation_required": True}, "请确认"),
        ({"status": "awaiting_confirmation"}, "请确认"),
        ({"status": "pending"}, "等待处理"),
        ({"status": "processing"}, "正在生成"),
        ({"status": "done"}, "已生成完成"),
        ({"status": "failed"}, "未成功"),
        ({"status": "unknown"}, "核实"),
    ],
)
def test_generation_status_messages(result, expected):
    assert expected in generation_message(result)


def run_loop(decisions, variant="v2", tool_result=None):
    st = state(variant, "photo-1")
    agent = PhotoAgent(SimpleNamespace(info={}))
    agent._execute_tool = AsyncMock(
        return_value=tool_result or {"ok": True, "message": "完成"}
    )
    llm = AsyncMock(side_effect=[(d, {"total_tokens": 2}) for d in decisions])
    events = []
    asyncio.run(
        _run_llm_loop(
            agent,
            SimpleNamespace(llm_decide=llm),
            st.user_id,
            st.original_query,
            st,
            events,
            lambda kind, payload: events.append({"type": kind, **payload}),
            time.monotonic(),
            0,
        )
    )
    return st, events, llm, agent


def call(name):
    return {
        "id": "call-1",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


@pytest.mark.parametrize("variant", ["control", "v2"])
def test_reasoning_only_response_is_not_public_or_persisted(variant):
    st, events, _, _ = run_loop(
        [{"reasoning_content": "SECRET_THOUGHT", "tool_calls": []}], variant
    )
    assert "SECRET_THOUGHT" not in json.dumps([events, st.to_json()])
    assert events[-1]["type"] == "final"


def test_unavailable_tool_is_not_executed_and_plain_answer_still_works():
    _, events, _, agent = run_loop(
        [
            {"tool_calls": [call("fallback_search")]},
            {"content": "请描述要找的照片", "tool_calls": []},
        ]
    )
    agent._execute_tool.assert_not_awaited()
    assert any(
        e.get("result", {}).get("error_type") == "tool_not_available" for e in events
    )
    assert events[-1]["message"] == "请描述要找的照片"


@pytest.mark.parametrize("variant", ["control", "v2"])
def test_generation_stops_before_model_can_claim_completion(variant):
    _, events, llm, agent = run_loop(
        [
            {
                "content": "我已经生成完成",
                "reasoning_content": "SECRET_THOUGHT",
                "tool_calls": [call("apply_skill"), call("final_answer")],
            },
        ],
        variant,
        {"ok": True, "status": "awaiting_confirmation", "confirmation_required": True},
    )
    assert llm.await_count == agent._execute_tool.await_count == 1
    assert "请确认" in events[-1]["message"]
    assert "我已经生成完成" not in json.dumps(events, ensure_ascii=False)


def test_control_final_answer_remains_supported():
    _, events, _, agent = run_loop([{"tool_calls": [call("final_answer")]}], "control")
    agent._execute_tool.assert_awaited_once()
    assert events[-1]["message"] == "完成"


def test_provider_adapter_drops_reasoning(monkeypatch):
    from app.services import agent_llm

    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "choices": [
                {"message": {"reasoning_content": "SECRET_THOUGHT", "content": None}}
            ],
            "usage": {"total_tokens": 7},
        },
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(agent_llm, "_is_mock_llm", lambda: False)
    monkeypatch.setattr(agent_llm.httpx, "AsyncClient", lambda **kwargs: client)

    async def direct(fn):
        return await fn()

    monkeypatch.setattr(agent_llm.agent_llm_breaker, "call", direct)
    decision, usage = asyncio.run(agent_llm._llm_decide([], []))
    assert decision == {"role": "assistant", "content": "", "tool_calls": []}
    assert usage["total_tokens"] == 7


def test_mock_adapter_can_finish_with_no_tools(monkeypatch):
    from app.services import agent_llm

    monkeypatch.setattr(agent_llm, "_is_mock_llm", lambda: True)
    decision, _ = asyncio.run(agent_llm._llm_decide([], []))
    assert decision["content"]
    assert decision["tool_calls"] == []


def test_v2_policy_errors_do_not_recommend_hidden_tools():
    from app.services.agent_execution import _apply_tool_policies

    agent = PhotoAgent(SimpleNamespace(info={}))
    st = state()
    st.search_attempts = agent.constraints.max_searches
    for name in ("search_photos", "ask_clarification"):
        result = _apply_tool_policies(agent, name, {}, st)
        assert result["ok"] is False
        assert "fallback_search" not in result["hint"]


def test_provider_error_does_not_echo_response(monkeypatch):
    from app.services import agent_llm

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = SimpleNamespace(status_code=400, text="SECRET_THOUGHT")
    monkeypatch.setattr(agent_llm, "_is_mock_llm", lambda: False)
    monkeypatch.setattr(agent_llm.httpx, "AsyncClient", lambda **kwargs: client)

    async def direct(fn):
        return await fn()

    monkeypatch.setattr(agent_llm.agent_llm_breaker, "call", direct)
    with pytest.raises(RuntimeError, match="Agent LLM HTTP 400") as exc:
        asyncio.run(agent_llm._llm_decide([], []))
    assert "SECRET_THOUGHT" not in str(exc.value)


def test_package_title_options_are_advertised_validated_and_bound_to_idempotency():
    from app.services.agent_registry import DEFAULT_REGISTRY
    from app.services.agent_execution import _prepare_arguments, _apply_tool_policies

    spec = DEFAULT_REGISTRY.get("apply_skill")
    assert spec.parameters["properties"]["package_options"]["properties"]["title_mode"][
        "enum"
    ] == ["auto", "none", "exact"]
    st = state(selected=str(uuid4()))
    agent = PhotoAgent(SimpleNamespace(info={}))

    def prepare(options):
        args, error = _prepare_arguments(
            agent,
            st.user_id,
            "apply_skill",
            {
                "photo_id": st.confirmed_photo_id,
                "package_options": options,
            },
            st,
        )
        assert error is None
        assert _apply_tool_policies(agent, "apply_skill", args, st) is None
        assert args["require_confirmation"]
        return args["idempotency_key"]

    first = prepare({"title_mode": "exact", "title": "山间"})
    assert first == prepare({"title": "山间", "title_mode": "exact"})
    assert first != prepare({"title_mode": "none"})
    assert first != prepare({"title_mode": "exact", "title": "海边"})
    _, error = _prepare_arguments(
        agent,
        st.user_id,
        "apply_skill",
        {
            "photo_id": st.confirmed_photo_id,
            "package_options": {"title_mode": "exact"},
        },
        st,
    )
    assert error["error_type"] == "invalid_arguments"
