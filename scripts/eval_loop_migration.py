"""Run frozen multi-turn cases with real decisions and isolated business tools.

Use --baseline to load the pre-migration app snapshot in a separate process.
Budget reservations are persisted before HTTP; uncertain requests keep reservation.
"""

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / ".project-to-act/tasks/S6-LOOP-20260909"


async def run(args):
    source = TASK / "baseline" if args.baseline else ROOT
    sys.path.insert(0, str(source))
    from app.config import settings
    from app.services import agent as module, agent_execution, agent_runtime
    from app.services.agent import PhotoAgent
    from app.services.agent_registry import _build_registry
    from app.services.agent_state import AgentState, AgentConstraints
    import httpx

    if module._is_mock_llm():
        raise RuntimeError("real evaluation requires configured model credentials")

    if (
        settings.qwen_chat_model != "qwen-plus"
        or settings.dashscope_chat_url
        != "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    ):
        raise RuntimeError("unpriced model or endpoint; no calls made")
    dataset_path = ROOT / args.dataset
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
    ]
    cases = [c for c in cases if c["split"] == args.split][: args.limit]
    ledger_path = TASK / "evidence/model-budget.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = (
        json.loads(ledger_path.read_text())
        if ledger_path.exists()
        else {
            "limit_cny": args.budget_cny,
            "spent_or_reserved_cny": 0,
            "requests": [],
            "price_source": "https://help.aliyun.com/zh/model-studio/qwen-plus",
            "input_per_million_cny": 0.8,
            "output_per_million_cny": 2,
        }
    )
    original_post = httpx.AsyncClient.post
    if ledger["limit_cny"] != args.budget_cny:
        raise RuntimeError("existing ledger cap differs from requested cap")
    request_count = 0
    call_times = []

    async def post(client, url, **kw):
        nonlocal request_count
        if str(url) != settings.dashscope_chat_url:
            raise RuntimeError("evaluation forbids other network requests")
        body = kw["json"]
        # Conservative UTF-8 byte bound, plus chat/tool protocol overhead.
        bound = len(json.dumps(body, ensure_ascii=False).encode()) + 8192
        if bound > 100000:
            raise RuntimeError("evaluation prompt bound exceeded")
        body["enable_thinking"] = False
        max_output = int(body.get("max_tokens", 800))
        reserve = (bound * 0.8 + max_output * 2) / 1000000
        if ledger["spent_or_reserved_cny"] + reserve > ledger["limit_cny"]:
            raise RuntimeError("evaluation budget exhausted")
        entry = {
            "reservation_cny": reserve,
            "status": "reserved",
            "baseline": args.baseline,
        }
        ledger["requests"].append(entry)
        ledger["spent_or_reserved_cny"] += reserve
        ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        request_count += 1
        started = time.perf_counter()
        try:
            response = await original_post(client, url, **kw)
            entry["http_status"] = response.status_code
            entry["resolved_model"] = response.json().get("model")
            usage = response.json().get("usage", {})
            if (
                response.status_code == 200
                and "prompt_tokens" in usage
                and "completion_tokens" in usage
            ):
                actual = (
                    usage["prompt_tokens"] * 0.8 + usage["completion_tokens"] * 2
                ) / 1000000
                ledger["spent_or_reserved_cny"] += actual - reserve
                entry.update(status="accounted", cost_cny=actual, usage=usage)
            else:
                entry["status"] = "uncertain"
            return response
        finally:
            entry["elapsed_ms"] = (time.perf_counter() - started) * 1000
            call_times.append(entry["elapsed_ms"])
            ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    source_hashes = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (source / "app/services").glob("agent*.py")
    }
    rows = []
    report_path = (
        TASK
        / "evidence"
        / f"{'old' if args.baseline else 'new'}-{args.split}{args.suffix}.json"
    )
    if report_path.exists():
        raise RuntimeError("report already exists; refuse implicit paid rerun")
    for repeat in range(args.repeat):
        for case in cases:
            fixtures = []
            registry = _build_registry()
            plan_queries = {}

            async def search(**kw):
                fixtures.append(
                    {
                        "tool": "search",
                        "arguments": {
                            k: str(v) if k in {"from_date", "to_date"} else v
                            for k, v in kw.items()
                            if k not in {"db", "user_id"}
                        },
                    }
                )
                query = kw.get("query", "全部相册")
                subject = "狗" if "狗" in query else "猫"
                plan = (
                    kw.get("plan_id") or hashlib.sha256(query.encode()).hexdigest()[:16]
                )
                plan_queries[plan] = query
                excluded = set(kw.get("exclude_photo_ids", []))
                offset = int(kw.get("cursor") or 0)
                pool = [
                    {
                        "id": f"10000000-0000-0000-0000-{i:012d}",
                        "ai_description": f"合成{subject}照片{i}",
                    }
                    for i in range(
                        1 if subject == "猫" else 101, 13 if subject == "猫" else 113
                    )
                ]
                items = [p for p in pool[offset:] if p["id"] not in excluded][
                    : min(kw.get("limit", 5), 5)
                ]
                next_offset = offset + len(items)
                return {
                    "ok": True,
                    "items": items,
                    "total": len(items),
                    "index_coverage": {"complete": True},
                    "_search_plan_id": plan,
                    "next_cursor": str(next_offset)
                    if next_offset < len(pool)
                    else None,
                    "browse_scope": "clues"
                    if kw.get("feedback_level", 0) >= 2
                    else "matches",
                }

            async def browse(**kw):
                result = await search(**kw)
                result["browse_scope"] = "all"
                return result

            async def generation(**kw):
                fixtures.append(
                    {
                        "tool": "generation",
                        "arguments": {"photo_id": str(kw.get("photo_id"))},
                    }
                )
                return {
                    "ok": True,
                    "confirmation_required": True,
                    "status": "awaiting_confirmation",
                }

            async def recommend(**kw):
                fixtures.append({"tool": "recommend", "arguments": {}})
                return {"ok": True, "skills": [], "hint": "暂无推荐"}

            async def maintain(agent, deps, uid, name, kw, result, state):
                if result.get("_search_plan_id"):
                    state.active_search["plan_id"] = result.pop("_search_plan_id")
                return [], None, None

            for name in ("search_photos", "fallback_search"):
                registry.get(name).fn = search
            registry.get("browse_candidates").fn = browse
            registry.get("apply_skill").fn = generation
            registry.get("recommend_skills").fn = recommend
            registry.get("read_workspace").fn = AsyncMock(
                return_value={"ok": True, "selections": []}
            )
            st = AgentState(
                UUID(int=1), UUID(int=2), "", agent_variant=args.agent_variant
            )
            db = SimpleNamespace(
                info={},
                execute=AsyncMock(
                    return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
                ),
            )
            agent = PhotoAgent(
                db,
                registry=registry,
                constraints=AgentConstraints()
                if args.current_settings
                else AgentConstraints(max_steps=4, max_time_seconds=45),
            )
            tool_times = []
            execute_original = agent._execute_tool

            async def timed_execute(*values, **keywords):
                started_tool = time.perf_counter()
                try:
                    return await execute_original(*values, **keywords)
                finally:
                    tool_times.append((time.perf_counter() - started_tool) * 1000)

            agent._execute_tool = timed_execute
            observations = []
            failures = []
            with ExitStack() as stack:
                stack.enter_context(patch.object(httpx.AsyncClient, "post", post))
                stack.enter_context(
                    patch.object(agent_execution, "_run_search_maintenance", maintain)
                )
                stack.enter_context(
                    patch(
                        "app.services.search_candidate_pool.begin_candidate_search",
                        AsyncMock(return_value=None),
                    )
                )
                if hasattr(agent_runtime, "log_event"):
                    stack.enter_context(
                        patch.object(agent_runtime, "log_event", AsyncMock())
                    )
                stack.enter_context(patch.object(module, "browse_candidates", browse))
                for turn_index, turn in enumerate(case["turns"]):
                    if turn.get("select_first") and st.last_search_items:
                        st.confirmed_photo_id = st.last_search_items[0]["id"]
                    before = request_count
                    timing_begin = len(tool_times)
                    tool_begin = len(fixtures)
                    begin_time = time.perf_counter()
                    events = []
                    error = None
                    try:
                        _, events = await asyncio.wait_for(
                            agent.run(st.user_id, turn["input"], initial_state=st),
                            agent.constraints.max_time_seconds + 10,
                        )
                    except Exception as exc:
                        error = type(exc).__name__
                    obs = {
                        "model_calls": request_count - before,
                        "decision_ms": sum(call_times[before:request_count]),
                        "tool_executor_ms": sum(tool_times[timing_begin:]),
                        "elapsed_ms": (time.perf_counter() - begin_time) * 1000,
                        "tools": [
                            e["payload"]["tool"]
                            for e in events
                            if e["type"] == "tool_call"
                        ],
                        "action_arguments": [
                            e["payload"] for e in events if e["type"] == "tool_call"
                        ],
                        "answer": [
                            e["payload"]
                            for e in events
                            if e["type"] in {"final", "clarify"}
                        ],
                        "fixtures": fixtures[tool_begin:],
                        "query": st.active_search.get("resolved_query", ""),
                        "ids": [p["id"] for p in st.last_search_items],
                        "selected": st.confirmed_photo_id,
                        "rejected": sorted(st.rejected_photo_ids),
                        "scope": st.search_feedback.get("scope"),
                        "clarified": any(e["type"] == "clarify" for e in events),
                        "error": error,
                    }
                    obs["clarification_review"] = (
                        "pending" if args.text_clarification else "not_requested"
                    )
                    # Expected fields are consumed only after execution.
                    expected = turn["expected"]
                    bad = []
                    if error:
                        bad.append("execution")
                    for term in expected.get("query_terms", []):
                        if term not in obs["query"]:
                            bad.append("query:" + term)
                    for term in expected.get("query_absent", []):
                        if term in obs["query"]:
                            bad.append("query_absent:" + term)
                    if (
                        "clarified" in expected
                        and not args.text_clarification
                        and obs["clarified"] != expected["clarified"]
                    ):
                        bad.append("clarification")
                    if "rejected_ids" in expected and obs["rejected"] != sorted(
                        expected["rejected_ids"]
                    ):
                        bad.append("wrong_rejected_ids")
                    if expected.get("search") and not any(
                        f["tool"] == "search" for f in obs["fixtures"]
                    ):
                        bad.append("missing_search")
                    if expected.get("no_search") and any(
                        f["tool"] == "search" for f in obs["fixtures"]
                    ):
                        bad.append("unexpected_search")
                    if expected.get("no_generation") and any(
                        f["tool"] == "generation" for f in obs["fixtures"]
                    ):
                        bad.append("unsafe_generation")
                    if expected.get("selected_empty") and obs["selected"]:
                        bad.append("old_selection")
                    if (
                        expected.get("selected_kept")
                        and obs["selected"] != "10000000-0000-0000-0000-000000000001"
                    ):
                        bad.append("lost_selection")
                    if (
                        "rejected_count" in expected
                        and len(obs["rejected"]) != expected["rejected_count"]
                    ):
                        bad.append("rejection")
                    if (
                        expected.get("no_repeat")
                        and turn_index
                        and set(obs["ids"]) & set(observations[-1]["ids"])
                    ):
                        bad.append("duplicate_results")
                    if "scope" in expected and obs["scope"] != expected["scope"]:
                        bad.append("scope")
                    obs["failures"] = bad
                    observations.append(obs)
                    failures.extend(bad)
            rows.append(
                {
                    "id": case["id"],
                    "repeat": repeat,
                    "passed": not failures,
                    "failures": failures,
                    "turns": observations,
                }
            )
            report = {
                "clarification_scoring": "manual_pending"
                if args.text_clarification
                else "tool_event",
                "baseline": args.baseline,
                "agent_variant": args.agent_variant,
                "constraints": vars(agent.constraints),
                "split": args.split,
                "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                "source_hashes": source_hashes,
                "count": len(rows),
                "passed": sum(r["passed"] for r in rows),
                "rows": rows,
                "spent_or_reserved_cny": ledger["spent_or_reserved_cny"],
                "model_calls": request_count,
                "limitations": [
                    "synthetic single-author cases",
                    "business tools are fixtures",
                    "not a retrieval or production gate",
                ],
            }
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(case["id"], repeat, "PASS" if not failures else failures, flush=True)
    print(
        json.dumps(
            {
                "count": len(rows),
                "passed": sum(r["passed"] for r in rows),
                "cost": ledger["spent_or_reserved_cny"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="tests/eval/agent/loop_migration_v1.jsonl")
    parser.add_argument("--text-clarification", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument(
        "--split", choices=["development", "validation"], default="development"
    )
    parser.add_argument("--repeat", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--suffix", default="")
    parser.add_argument("--task-dir", default=str(TASK))
    parser.add_argument("--budget-cny", type=float, default=10)
    parser.add_argument("--agent-variant", choices=["control", "v2"], default="v2")
    parser.add_argument("--current-settings", action="store_true")
    import os

    args = parser.parse_args()
    TASK = Path(args.task_dir).resolve()
    if not TASK.is_relative_to(ROOT) or not 0 < args.budget_cny <= 30:
        raise RuntimeError("invalid evaluation output directory or budget")

    lock_path = TASK / "evidence/evaluation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        asyncio.run(run(args))
    finally:
        os.close(fd)
        lock_path.unlink()
