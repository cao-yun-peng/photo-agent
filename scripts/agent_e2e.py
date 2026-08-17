"""Run real HTTP end-to-end checks against a deployed Photo Agent API.

Unlike ``scripts/agent_eval.py``, this runner does not replace Agent tools with
stubs. Requests pass through authentication, Redis locking, Agent decisions,
PostgreSQL session persistence, and the production tool implementations.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx


SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "cookie",
    "id_token",
    "refresh_token",
    "token",
}
URL_KEYS = {"image_url", "result_url", "thumb_url", "upload_url", "url"}


def load_dataset(path: str | Path) -> dict[str, Any]:
    dataset = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: dict[str, Any]) -> None:
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("E2E 数据集必须包含非空 cases")
    ids = [case.get("id") for case in cases]
    if any(not case_id for case_id in ids):
        raise ValueError("每个 E2E 用例必须包含 id")
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"E2E 数据集存在重复 ID: {duplicates}")
    for case in cases:
        turns = case.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{case['id']} 必须包含非空 turns")
        for index, turn in enumerate(turns, start=1):
            if not isinstance(turn.get("query"), str) or not turn["query"]:
                raise ValueError(f"{case['id']} turn {index} 缺少 query")
            if turn.get("transport", "run") not in {"run", "stream"}:
                raise ValueError(f"{case['id']} turn {index} transport 无效")
            if not isinstance(turn.get("expected", {}), dict):
                raise ValueError(f"{case['id']} turn {index} expected 必须是对象")


def parse_sse_lines(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if not stripped.startswith("data:"):
            continue
        payload = stripped[len("data:") :].strip()
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ValueError("SSE data 必须是 JSON 对象")
        events.append(event)
    return events


def _parse_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def collect_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_call":
            continue
        payload = event.get("payload", {})
        calls.append(
            {
                "tool": str(payload.get("tool", "")),
                "arguments": _parse_tool_arguments(payload.get("arguments", {})),
            }
        )
    return calls


def collect_tool_results(events: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_result":
            continue
        payload = event.get("payload", {})
        if payload.get("tool") == tool and isinstance(payload.get("result"), dict):
            results.append(payload["result"])
    return results


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    position = 0
    for item in actual:
        if position < len(expected) and item == expected[position]:
            position += 1
    return position == len(expected)


def _value_matches(actual: Any, rule: Any) -> bool:
    if not isinstance(rule, dict):
        return actual == rule
    if "equals" in rule and actual != rule["equals"]:
        return False
    if "one_of" in rule and actual not in rule["one_of"]:
        return False
    if rule.get("not_empty") and actual in (None, "", [], {}):
        return False
    text = str(actual).lower()
    if "contains" in rule and str(rule["contains"]).lower() not in text:
        return False
    return True


def _final_message(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") == "final":
            return str(event.get("payload", {}).get("message", ""))
        if event.get("type") == "clarify":
            return str(event.get("payload", {}).get("question", ""))
    return ""


def evaluate_turn(
    *,
    expected: dict[str, Any],
    http_status: int,
    response_body: dict[str, Any],
    events: list[dict[str, Any]],
    previous_session_id: str | None,
) -> list[str]:
    acceptable_outcomes = expected.get("acceptable_outcomes", [])
    if acceptable_outcomes:
        common = {
            key: value
            for key, value in expected.items()
            if key != "acceptable_outcomes"
        }
        outcome_failures: list[list[str]] = []
        for outcome in acceptable_outcomes:
            failures = evaluate_turn(
                expected={**common, **outcome},
                http_status=http_status,
                response_body=response_body,
                events=events,
                previous_session_id=previous_session_id,
            )
            if not failures:
                return []
            outcome_failures.append(failures)
        details = " || ".join(
            f"方案 {index}: {'; '.join(failures)}"
            for index, failures in enumerate(outcome_failures, start=1)
        )
        return [f"未满足任一允许结果: {details}"]

    failures: list[str] = []
    expected_http = int(expected.get("http_status", 200))
    if http_status != expected_http:
        failures.append(f"HTTP 状态不匹配: expected={expected_http}, actual={http_status}")
        return failures
    if http_status >= 400:
        return failures

    actual_status = response_body.get("status")
    expected_status = expected.get("response_status")
    if expected_status and actual_status != expected_status:
        failures.append(
            f"Agent 状态不匹配: expected={expected_status}, actual={actual_status}"
        )

    session_id = str(response_body.get("session_id", ""))
    if expected.get("same_session_as_previous"):
        if not previous_session_id or session_id != previous_session_id:
            failures.append(
                "会话未正确续接: "
                f"previous={previous_session_id}, actual={session_id or None}"
            )
    if expected.get("session_id_required") and not session_id:
        failures.append("响应缺少 session_id")

    event_types = [str(event.get("type", "")) for event in events]
    for event_type in expected.get("required_event_types", []):
        if event_type not in event_types:
            failures.append(f"缺少事件: {event_type}")
    for event_type in expected.get("forbidden_event_types", []):
        if event_type in event_types:
            failures.append(f"出现禁止事件: {event_type}")

    tool_calls = collect_tool_calls(events)
    actual_tools = [call["tool"] for call in tool_calls]
    if "max_tool_calls" in expected and len(tool_calls) > int(expected["max_tool_calls"]):
        failures.append(
            f"工具调用过多: expected<={expected['max_tool_calls']}, actual={len(tool_calls)}"
        )
    required_tools = list(expected.get("required_tools_ordered", []))
    if required_tools and not _is_subsequence(required_tools, actual_tools):
        failures.append(
            f"工具顺序不匹配: expected subsequence={required_tools}, actual={actual_tools}"
        )
    forbidden_tools = set(expected.get("forbidden_tools", []))
    violations = sorted(forbidden_tools.intersection(actual_tools))
    if violations:
        failures.append(f"调用了禁止工具: {violations}")

    for tool_name, checks in expected.get("tool_arguments", {}).items():
        call = next((item for item in tool_calls if item["tool"] == tool_name), None)
        if call is None:
            failures.append(f"无法检查参数，未调用工具: {tool_name}")
            continue
        for argument_name, rule in checks.items():
            actual = call["arguments"].get(argument_name)
            if argument_name not in call["arguments"] or not _value_matches(actual, rule):
                failures.append(
                    f"工具参数不匹配: {tool_name}.{argument_name}, "
                    f"expected={rule!r}, actual={actual!r}"
                )

    for assertion in expected.get("tool_results", []):
        tool_name = assertion["tool"]
        results = collect_tool_results(events, tool_name)
        if not results:
            failures.append(f"缺少工具结果: {tool_name}")
            continue
        result = results[-1]
        if "ok" in assertion and result.get("ok") is not assertion["ok"]:
            failures.append(
                f"工具结果 ok 不匹配: {tool_name}, "
                f"expected={assertion['ok']}, actual={result.get('ok')}"
            )
        items = result.get("items")
        if "min_items" in assertion:
            if not isinstance(items, list) or len(items) < int(assertion["min_items"]):
                failures.append(
                    f"工具结果数量过少: {tool_name}, "
                    f"expected>={assertion['min_items']}, actual={len(items) if isinstance(items, list) else None}"
                )
        if "max_items" in assertion:
            if not isinstance(items, list) or len(items) > int(assertion["max_items"]):
                failures.append(
                    f"工具结果数量过多: {tool_name}, "
                    f"expected<={assertion['max_items']}, actual={len(items) if isinstance(items, list) else None}"
                )
        for field in assertion.get("required_fields", []):
            if result.get(field) in (None, "", [], {}):
                failures.append(f"工具结果缺少字段: {tool_name}.{field}")

    final_message = _final_message(events)
    for token in expected.get("final_contains_all", []):
        if str(token) not in final_message:
            failures.append(f"最终回复缺少内容: {token}")
    contains_any = [str(token) for token in expected.get("final_contains_any", [])]
    if contains_any and not any(token in final_message for token in contains_any):
        failures.append(f"最终回复未包含任一允许内容: {contains_any}")
    for token in expected.get("final_excludes", []):
        if str(token) in final_message:
            failures.append(f"最终回复包含禁止内容: {token}")
    if expected.get("final_not_empty") and not final_message.strip():
        failures.append("最终回复为空")
    return failures


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_KEYS:
                output[key] = "<redacted>"
            elif lowered in URL_KEYS or lowered.endswith("_url"):
                output[key] = "<redacted-url>" if item else item
            else:
                output[key] = redact(item)
        return output
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


async def _post_run(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    response = await client.post("/agent/run", json=payload)
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"raw": response.text[:2000]}
    events = body.get("events", []) if isinstance(body, dict) else []
    return response.status_code, body, events


async def _post_stream(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    lines: list[str] = []
    async with client.stream("POST", "/agent/stream", json=payload) as response:
        async for line in response.aiter_lines():
            lines.append(line)
        status_code = response.status_code
    events = parse_sse_lines(lines)
    done = next((event for event in reversed(events) if event.get("type") == "done"), None)
    body = dict(done.get("payload", {})) if done else {}
    if not body:
        error = next(
            (event for event in reversed(events) if event.get("type") == "error"),
            None,
        )
        if error:
            body = dict(error.get("payload", {}))
    return status_code, body, events


async def run_case(
    client: httpx.AsyncClient,
    case: dict[str, Any],
    *,
    include_mutations: bool,
) -> dict[str, Any]:
    if case.get("mutates_data") and not include_mutations:
        return {
            "id": case["id"],
            "status": "skipped",
            "reason": "需要 --include-mutations",
            "turns": [],
        }

    previous_session_id: str | None = None
    turn_reports: list[dict[str, Any]] = []
    case_failures: list[str] = []
    started = time.monotonic()
    for turn_index, turn in enumerate(case["turns"], start=1):
        session_mode = turn.get("session", "new" if turn_index == 1 else "previous")
        payload: dict[str, Any] = {"query": turn["query"]}
        if session_mode == "previous":
            if not previous_session_id:
                failure = f"turn {turn_index}: 没有可续接的 previous session"
                case_failures.append(failure)
                turn_reports.append({"turn": turn_index, "failures": [failure]})
                break
            payload["session_id"] = previous_session_id
        elif session_mode == "random":
            payload["session_id"] = str(uuid4())

        turn_started = time.monotonic()
        try:
            if turn.get("transport", "run") == "stream":
                http_status, body, events = await _post_stream(client, payload)
            else:
                http_status, body, events = await _post_run(client, payload)
            failures = evaluate_turn(
                expected=turn.get("expected", {}),
                http_status=http_status,
                response_body=body,
                events=events,
                previous_session_id=previous_session_id,
            )
        except Exception as exc:  # noqa: BLE001
            http_status, body, events = 0, {}, []
            failures = [f"{type(exc).__name__}: {exc}"]

        response_session_id = body.get("session_id") if isinstance(body, dict) else None
        if response_session_id:
            previous_session_id = str(response_session_id)
        case_failures.extend(f"turn {turn_index}: {failure}" for failure in failures)
        turn_reports.append(
            {
                "turn": turn_index,
                "transport": turn.get("transport", "run"),
                "query": turn["query"],
                "http_status": http_status,
                "elapsed_ms": int((time.monotonic() - turn_started) * 1000),
                "passed": not failures,
                "failures": failures,
                "response": redact(body),
                "events": redact(events),
            }
        )
        if failures and turn.get("stop_on_failure", True):
            break

    return {
        "id": case["id"],
        "name": case.get("name", case["id"]),
        "tags": case.get("tags", []),
        "status": "passed" if not case_failures else "failed",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "failures": case_failures,
        "turns": turn_reports,
    }


async def preflight(client: httpx.AsyncClient) -> dict[str, Any]:
    live = await client.get("/live")
    ready = await client.get("/ready")
    me = await client.get("/auth/me")
    result = {
        "live": {"status_code": live.status_code, "body": live.json()},
        "ready": {"status_code": ready.status_code, "body": ready.json()},
        "auth_me": {"status_code": me.status_code, "body": me.json()},
    }
    if live.status_code != 200:
        raise ValueError(f"API liveness 失败: HTTP {live.status_code}")
    if ready.status_code != 200:
        raise ValueError(f"API readiness 失败: HTTP {ready.status_code} {ready.text[:300]}")
    me_body = result["auth_me"]["body"]
    if me.status_code != 200 or not isinstance(me_body, dict) or not me_body.get("id"):
        raise ValueError(f"测试 JWT 无效: HTTP {me.status_code} {me.text[:300]}")
    return redact(result)


async def run_suite(
    *,
    base_url: str,
    token: str,
    dataset_path: str,
    output_path: str,
    selected_cases: set[str] | None,
    include_mutations: bool,
    trust_env: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    dataset_file = Path(dataset_path)
    dataset = load_dataset(dataset_file)
    cases = [
        case
        for case in dataset["cases"]
        if selected_cases is None or case["id"] in selected_cases
    ]
    if selected_cases:
        missing = sorted(selected_cases - {case["id"] for case in cases})
        if missing:
            raise ValueError(f"未知 E2E 用例: {missing}")

    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(timeout_seconds, connect=5.0)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        trust_env=trust_env,
    ) as client:
        preflight_result = await preflight(client)
        results = []
        for case in cases:
            print(f"运行 {case['id']}: {case.get('name', '')}")
            result = await run_case(
                client,
                case,
                include_mutations=include_mutations,
            )
            results.append(result)
            print(f"  {result['status'].upper()} ({result.get('elapsed_ms', 0)}ms)")

    passed = sum(result["status"] == "passed" for result in results)
    failed = sum(result["status"] == "failed" for result in results)
    skipped = sum(result["status"] == "skipped" for result in results)
    report = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset": str(dataset_file),
            "dataset_version": dataset.get("version"),
            "dataset_role": dataset.get("dataset_role"),
            "dataset_sha256": hashlib.sha256(dataset_file.read_bytes()).hexdigest(),
            "base_url": base_url.rstrip("/"),
            "real_http": True,
            "real_tools": True,
            "no_retry": True,
            "include_mutations": include_mutations,
        },
        "preflight": preflight_result,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "gate_passed": failed == 0 and passed > 0,
        },
        "results": results,
    }
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def _is_loopback(base_url: str) -> bool:
    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def validate_jwt_token(token: str) -> str:
    """在构造 Authorization 头之前拒绝占位文本和明显无效的 JWT。"""
    normalized = token.strip()
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "PHOTO_EVAL_JWT 包含非 ASCII 字符，当前值很可能是‘隔离测试用户 JWT’"
            "之类的中文占位文本，请换成真实 JWT。"
        ) from exc
    parts = normalized.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError(
            "PHOTO_EVAL_JWT 不是标准的三段式 JWT，请为隔离测试用户重新签发令牌。"
        )
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Agent 真实 HTTP 端到端测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token",
        default=os.getenv("PHOTO_EVAL_JWT"),
        help="隔离测试用户 JWT；默认读取 PHOTO_EVAL_JWT",
    )
    parser.add_argument(
        "--dataset",
        default="tests/eval/agent_e2e_dataset.json",
    )
    parser.add_argument("--output", default="artifacts/agent-e2e-result.json")
    parser.add_argument("--cases", help="逗号分隔的用例 ID")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--trust-env", action="store_true")
    parser.add_argument(
        "--include-mutations",
        action="store_true",
        help="运行会创建生成任务并可能产生模型费用的用例",
    )
    parser.add_argument(
        "--confirm-test-account",
        action="store_true",
        help="确认 JWT 属于隔离测试账号，不是生产用户",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许连接非 loopback 地址；仅应用于明确的测试环境",
    )
    args = parser.parse_args()

    if not args.token:
        parser.error("请通过 PHOTO_EVAL_JWT 或 --token 提供隔离测试用户 JWT")
    if not args.confirm_test_account:
        parser.error("必须传 --confirm-test-account，确认不会使用生产用户")
    if not args.allow_remote and not _is_loopback(args.base_url):
        parser.error("默认只允许本机 API；测试远端环境需显式传 --allow-remote")

    selected = {item.strip() for item in args.cases.split(",") if item.strip()} if args.cases else None
    try:
        token = validate_jwt_token(args.token)
        report = asyncio.run(
            run_suite(
                base_url=args.base_url,
                token=token,
                dataset_path=args.dataset,
                output_path=args.output,
                selected_cases=selected,
                include_mutations=args.include_mutations,
                trust_env=args.trust_env,
                timeout_seconds=args.timeout,
            )
        )
    except (ValueError, httpx.HTTPError, json.JSONDecodeError) as exc:
        print(f"E2E 配置或预检失败: {type(exc).__name__}: {exc}")
        return 2

    summary = report["summary"]
    print(
        "Agent E2E: "
        f"{summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped"
    )
    print(f"报告: {args.output}")
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
