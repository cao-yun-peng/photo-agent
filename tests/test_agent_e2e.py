"""真实 Agent E2E runner 的离线回归测试，不访问网络。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.agent_e2e import (
    _post_stream,
    collect_tool_calls,
    evaluate_turn,
    load_dataset,
    parse_sse_lines,
    redact,
    run_case,
    validate_jwt_token,
)


def _events() -> list[dict]:
    return [
        {"type": "start", "payload": {}},
        {"type": "think", "payload": {"tokens_used": 10}},
        {
            "type": "tool_call",
            "payload": {
                "tool": "search_photos",
                "arguments": '{"query":"猫","result_mode":"best"}',
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "tool": "search_photos",
                "result": {
                    "ok": True,
                    "items": [{"id": "photo-1", "thumb_url": "signed"}],
                },
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "tool": "final_answer",
                "arguments": '{"message":"找到一张猫照片"}',
            },
        },
        {"type": "final", "payload": {"message": "找到一张猫照片"}},
    ]


def test_checked_in_agent_e2e_dataset_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    dataset = load_dataset(root / "tests/eval/agent_e2e_dataset.json")
    assert len(dataset["cases"]) == 11
    assert dataset["dataset_role"] == "e2e-regression"


def test_parse_sse_and_collect_tool_arguments() -> None:
    lines = [
        "data: {\"type\":\"start\",\"payload\":{}}",
        "",
        "data: {\"type\":\"done\",\"payload\":{\"status\":\"completed\"}}",
    ]
    events = parse_sse_lines(lines)
    assert [event["type"] for event in events] == ["start", "done"]
    calls = collect_tool_calls(_events())
    assert calls[0]["arguments"]["result_mode"] == "best"


def test_evaluate_turn_checks_real_tool_arguments_and_results() -> None:
    failures = evaluate_turn(
        expected={
            "http_status": 200,
            "response_status": "completed",
            "session_id_required": True,
            "required_event_types": ["start", "tool_result", "final"],
            "required_tools_ordered": ["search_photos", "final_answer"],
            "tool_arguments": {
                "search_photos": {"result_mode": {"equals": "best"}}
            },
            "tool_results": [
                {"tool": "search_photos", "ok": True, "min_items": 1, "max_items": 1}
            ],
            "final_contains_any": ["猫", "照片"],
        },
        http_status=200,
        response_body={"status": "completed", "session_id": "session-1"},
        events=_events(),
        previous_session_id=None,
    )
    assert failures == []


def test_evaluate_turn_reports_session_and_safety_failures() -> None:
    events = _events()
    failures = evaluate_turn(
        expected={
            "http_status": 200,
            "response_status": "completed",
            "same_session_as_previous": True,
            "forbidden_tools": ["search_photos"],
            "final_excludes": ["找到"],
        },
        http_status=200,
        response_body={"status": "completed", "session_id": "new-session"},
        events=events,
        previous_session_id="old-session",
    )
    assert any("会话未正确续接" in failure for failure in failures)
    assert any("禁止工具" in failure for failure in failures)
    assert any("禁止内容" in failure for failure in failures)


def test_evaluate_turn_accepts_structured_or_natural_language_clarification() -> None:
    expected = {
        "http_status": 200,
        "session_id_required": True,
        "final_not_empty": True,
        "acceptable_outcomes": [
            {
                "response_status": "active",
                "required_event_types": ["clarify"],
                "required_tools_ordered": ["ask_clarification"],
            },
            {
                "response_status": "completed",
                "required_event_types": ["final"],
                "max_tool_calls": 0,
                "final_contains_any": ["时间", "地点", "类型"],
            },
        ],
    }
    natural_events = [
        {"type": "start", "payload": {}},
        {
            "type": "final",
            "payload": {"message": "你想按时间、地点还是照片类型来找？"},
        },
    ]
    failures = evaluate_turn(
        expected=expected,
        http_status=200,
        response_body={"status": "completed", "session_id": "session-1"},
        events=natural_events,
        previous_session_id=None,
    )
    assert failures == []


def test_evaluate_turn_rejects_unhelpful_plain_final_as_clarification() -> None:
    failures = evaluate_turn(
        expected={
            "http_status": 200,
            "acceptable_outcomes": [
                {
                    "response_status": "active",
                    "required_event_types": ["clarify"],
                },
                {
                    "response_status": "completed",
                    "required_event_types": ["final"],
                    "max_tool_calls": 0,
                    "final_contains_any": ["时间", "地点", "类型"],
                },
            ],
        },
        http_status=200,
        response_body={"status": "completed", "session_id": "session-1"},
        events=[{"type": "final", "payload": {"message": "好的。"}}],
        previous_session_id=None,
    )
    assert len(failures) == 1
    assert "未满足任一允许结果" in failures[0]


def test_redact_removes_tokens_and_signed_urls() -> None:
    payload = {
        "token": "secret",
        "nested": {
            "thumb_url": "https://signed.example/photo",
            "message": "ok",
        },
    }
    assert redact(payload) == {
        "token": "<redacted>",
        "nested": {"thumb_url": "<redacted-url>", "message": "ok"},
    }


def test_validate_jwt_token_rejects_chinese_placeholder() -> None:
    with pytest.raises(ValueError, match="中文占位文本"):
        validate_jwt_token("隔离测试用户 JWT")


def test_validate_jwt_token_rejects_non_jwt_ascii_text() -> None:
    with pytest.raises(ValueError, match="三段式 JWT"):
        validate_jwt_token("test-token")


def test_validate_jwt_token_accepts_three_segments() -> None:
    assert validate_jwt_token(" header.payload.signature ") == "header.payload.signature"


@pytest.mark.asyncio
async def test_run_case_sends_previous_session_id_on_second_turn() -> None:
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "session_id": "session-1",
                    "status": "active",
                    "events": [{"type": "clarify", "payload": {"question": "找什么？"}}],
                },
            )
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "completed",
                "events": [{"type": "final", "payload": {"message": "完成"}}],
            },
        )

    case = {
        "id": "session-case",
        "turns": [
            {
                "query": "找照片",
                "expected": {"http_status": 200, "response_status": "active"},
            },
            {
                "query": "找猫",
                "session": "previous",
                "expected": {
                    "http_status": 200,
                    "response_status": "completed",
                    "same_session_as_previous": True,
                },
            },
        ],
    }
    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        report = await run_case(client, case, include_mutations=False)

    assert report["status"] == "passed"
    assert "session_id" not in requests[0]
    assert requests[1]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_post_stream_parses_done_payload() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        body = (
            'data: {"type":"start","payload":{}}\n\n'
            'data: {"type":"final","payload":{"message":"完成"}}\n\n'
            'data: {"type":"done","payload":'
            '{"session_id":"session-1","status":"completed"}}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        status, payload, events = await _post_stream(client, {"query": "hello"})

    assert status == 200
    assert payload == {"session_id": "session-1", "status": "completed"}
    assert [event["type"] for event in events] == ["start", "final", "done"]
