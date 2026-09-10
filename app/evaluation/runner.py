"""Sequential, isolated runners. Never imported by the application server."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from app.evaluation.scoring import routing_score, trajectory_score
from app.evaluation.contracts import RoutingCase, TrajectoryCase


def read_cases(path, split):
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        (RoutingCase if "context" in row else TrajectoryCase).model_validate(row)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id")
    selected = [row for row in rows if row["split"] == split]
    if not selected:
        raise ValueError("empty dataset split")
    return selected


def deny_network(*args, **kwargs):
    raise RuntimeError("network disabled in offline evaluation")


def serialize_plan(plan):
    if plan is None:
        return None
    data = asdict(plan)
    data["search"] = plan.search.as_dict() if plan.search else None
    data["clarification"] = {
        "question": plan.clarification_question,
        "options": plan.clarification_options,
    }
    return data


async def run_routing(case, mode="rule_router"):
    from app.services import query_parser, turn_resolver

    context = deepcopy(case["context"])
    expected = case["expected"]
    started = time.perf_counter()
    failure = None
    plan = None
    calls = 0
    original = turn_resolver._resolve_contextual_with_llm

    async def measured(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                query_parser,
                "local_today",
                lambda *a, **k: date.fromisoformat(case["reference_date"]),
            )
        )
        stack.enter_context(
            patch.object(turn_resolver, "_resolve_contextual_with_llm", measured)
        )
        if mode == "rule_router":
            stack.enter_context(patch.object(socket.socket, "connect", deny_network))
        try:
            if mode == "rule_router":
                context.pop("recent_messages", None)
                plan = turn_resolver.resolve_turn_by_rule(case["user_input"], **context)
            else:
                if turn_resolver._is_mock_llm():
                    raise ValueError(
                        "live routing requires configured model credentials"
                    )
                plan = await asyncio.wait_for(
                    turn_resolver.resolve_turn(case["user_input"], **context),
                    timeout=40,
                )
        except Exception as exc:
            failure = type(exc).__name__
    actual = serialize_plan(plan)
    failures = routing_score(expected, actual, mode)
    if failure:
        failures.append("execution_error:" + failure)
    return {
        "id": case["id"],
        "tags": case["tags"],
        "risk": case["risk"],
        "actual": actual,
        "failures": failures,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "model_calls": calls,
        "tokens": plan.model_tokens if plan else 0,
        "score_intent": mode != "rule_router" or expected["rule_outcome"] == "plan",
        "expected_intent": expected["intent"],
        "actual_intent": plan.intent if plan else None,
        "relation_ok": plan is not None and plan.relation == expected["relation"],
        "dangerous_fast_path": bool(
            plan
            and plan.can_use_search_fast_path
            and case["risk"] == "safety_critical"
            and failures
        ),
        "unnecessary_clarification": bool(
            plan and plan.needs_clarification and not expected["needs_clarification"]
        ),
        "missing_clarification": bool(
            expected["needs_clarification"]
            and (not plan or not plan.needs_clarification)
        ),
    }


async def run_trajectory(case, live=False):
    """Real orchestration/policies, scripted or real model, fixture tool functions.

    DB, candidate maintenance, Redis and feedback telemetry are replaced. No real
    generation or ownership guarantee is measured by this layer.
    """
    from app.services import (
        agent as agent_module,
        agent_execution,
        query_parser,
        turn_resolver,
    )
    from app.services.agent import PhotoAgent
    from app.services.agent_registry import _build_registry
    from app.services.agent_state import AgentConstraints, AgentState

    registry = _build_registry()
    responses = deepcopy(case.get("tool_fixtures", {}))
    invocations = []
    attempted = []
    simulated_writes = 0
    calls = 0
    token_count = 0
    fixture_errors = []
    decisions = []
    max_calls = case.get("max_model_calls", 8)
    # Do not give evaluators a usable database or queue connection.
    db = SimpleNamespace(
        info={},
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
        ),
    )
    allowed_argument_fields = {
        "query",
        "photo_id",
        "skill_id",
        "from_date",
        "to_date",
        "limit",
        "result_mode",
        "complete_result_set",
        "exclude_photo_ids",
        "require_confirmation",
    }

    def fixture(name):
        async def invoke(**kwargs):
            nonlocal simulated_writes
            invocations.append(
                {
                    "tool": name,
                    "arguments": json.loads(
                        json.dumps(
                            {
                                k: v
                                for k, v in kwargs.items()
                                if k in allowed_argument_fields
                            },
                            default=str,
                        )
                    ),
                }
            )
            if not responses.get(name):
                fixture_errors.append("missing_fixture:" + name)
                raise ValueError("fixture exhausted")
            result = deepcopy(responses[name].pop(0))
            if result.pop("raise_timeout", False):
                raise TimeoutError("fixture timeout")
            if name == "apply_skill" and result.get("ok"):
                # Count the simulated domain effect, not model claims.
                simulated_writes += int(not result.get("confirmation_required", False))
            return result

        return invoke

    for spec in registry._tools.values():
        if spec.name not in {"final_answer", "ask_clarification"}:
            spec.fn = fixture(spec.name)
    original_llm = agent_module._llm_decide
    original_contextual = turn_resolver._resolve_contextual_with_llm

    async def decide(messages, tools):
        nonlocal calls, token_count
        if calls >= max_calls:
            raise RuntimeError("evaluation model call budget exhausted")
        calls += 1
        if live:
            result, usage = await original_llm(messages, tools)
            token_count += usage.get("total_tokens", 0)
            return result, usage
        if not decisions:
            fixture_errors.append("script_exhausted")
            raise RuntimeError("script exhausted")
        return decisions.pop(0), {"total_tokens": 0}

    async def contextual(*args, **kwargs):
        nonlocal calls, token_count
        if not live:
            fixture_errors.append("unscripted_contextual_route")
            raise RuntimeError("contextual route requires dedicated L1 evaluation")
        if calls >= max_calls:
            fixture_errors.append("model_call_budget")
            raise RuntimeError("evaluation model call budget exhausted")
        calls += 1
        result, tokens = await original_contextual(*args, **kwargs)
        token_count += tokens
        return result, tokens

    st = AgentState(
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("00000000-0000-0000-0000-000000000002"),
        "",
        agent_variant=case.get("variant", "v2"),
    )
    permitted_state = {
        "confirmed_photo_id",
        "workflow_state",
        "last_search_items",
        "active_search",
    }
    for key, value in case.get("initial_state", {}).items():
        if key not in permitted_state:
            raise ValueError("unsupported initial state field")
        setattr(st, key, deepcopy(value))
    agent = PhotoAgent(
        db,
        registry=registry,
        constraints=AgentConstraints(max_steps=4, max_time_seconds=45),
    )
    started = time.perf_counter()
    turn_results = []

    async def maintain(agent, dependencies, user_id, tool_name, args, result, state):
        if result.get("_search_plan_id"):
            state.active_search["plan_id"] = result.pop("_search_plan_id")
        return [], None, None

    with ExitStack() as stack:
        stack.enter_context(patch.object(agent_module, "_llm_decide", decide))
        stack.enter_context(
            patch.object(
                query_parser,
                "local_today",
                lambda *a, **k: date.fromisoformat(case["reference_date"]),
            )
        )
        stack.enter_context(
            patch.object(
                agent_execution,
                "_run_search_maintenance",
                maintain,
            )
        )
        stack.enter_context(
            patch(
                "app.services.search_candidate_pool.begin_candidate_search",
                AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                agent_module, "browse_candidates", fixture("browse_candidates")
            )
        )
        stack.enter_context(
            patch.object(turn_resolver, "_resolve_contextual_with_llm", contextual)
        )
        if not live:
            stack.enter_context(patch.object(socket.socket, "connect", deny_network))
            stack.enter_context(
                patch.object(turn_resolver, "_is_mock_llm", lambda: False)
            )
            stack.enter_context(
                patch.object(turn_resolver, "_resolve_contextual_with_llm", contextual)
            )
        for turn in case["turns"]:
            decisions[:] = deepcopy(turn.get("decisions", []))
            begin = len(invocations)
            error = None
            events = []
            try:
                st, events = await asyncio.wait_for(
                    agent.run(st.user_id, turn["user_input"], initial_state=st),
                    timeout=50,
                )
            except Exception as exc:
                error = type(exc).__name__
            names = [e["payload"]["tool"] for e in events if e["type"] == "tool_call"]
            attempted.extend(names)
            terminal = next(
                (
                    e
                    for e in reversed(events)
                    if e["type"] in {"final", "clarify", "error"}
                ),
                None,
            )
            observation = {
                "attempted_tools": names,
                "invocations": invocations[begin:],
                "state": {
                    "workflow_state": st.workflow_state,
                    "rejected_batch_count": st.search_feedback.get(
                        "rejected_batch_count", 0
                    ),
                    "browse_scope": st.search_feedback.get("scope", "matches"),
                    "rejected_photo_ids": sorted(st.rejected_photo_ids),
                    "confirmed_photo_id": st.confirmed_photo_id,
                    "last_result_ids": [p["id"] for p in st.last_search_items],
                },
                "terminal": terminal["type"] if terminal else None,
                "final": terminal["payload"].get(
                    "message", terminal["payload"].get("question", "")
                )
                if terminal
                else "",
                "simulated_writes": simulated_writes,
                "execution_error": error
                or (fixture_errors[-1] if fixture_errors else None),
            }
            failures = trajectory_score(turn["expected"], observation)
            # Only persist contract facts, never raw model text/reasoning/events.
            observation.pop("final")
            turn_results.append({"actual": observation, "failures": failures})
    return {
        "id": case["id"],
        "tags": case["tags"],
        "risk": case["risk"],
        "turns": turn_results,
        "failures": [
            f"turn:{i}:{f}" for i, t in enumerate(turn_results) for f in t["failures"]
        ],
        "latency_ms": (time.perf_counter() - started) * 1000,
        "model_calls": calls if live else 0,
        "scripted_decisions": 0 if live else calls,
        "tokens": token_count,
        "simulated_writes": simulated_writes,
    }


def file_hashes(root):
    paths = list((root / "app" / "evaluation").glob("*.py"))
    paths += list((root / "app" / "services").glob("agent*.py"))
    paths += [
        root / "app/services/turn_resolver.py",
        root / "app/services/query_parser.py",
        root / "app/core/registry.py",
    ]
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
