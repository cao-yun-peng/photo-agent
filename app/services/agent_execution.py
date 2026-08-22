"""Internal tool execution pipeline for PhotoAgent.

The facade passes late-bound collaborators so existing patch paths keep working.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from app.config import settings
from app.core.logger import get_logger
from app.core.telemetry import set_current_span_attributes, start_span
from app.services.agent_intent import (
    _requested_user_selection_limit,
    _requests_complete_result_set,
)
from app.services.agent_messages import _search_coverage_complete
from app.services.agent_state import AgentState
from app.services.agent_tools import _classify_exception
from app.services.agent_workflow import transition_workflow
from app.services.metrics import metrics
from app.utils.json_parser import (
    extract_json_field_by_regex,
    parse_as_dict,
    parse_json_or_default,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class AgentExecutionDependencies:
    """Late-bound collaborators kept patchable through app.services.agent."""

    candidate_pool_key: Callable[..., str]
    enqueue_index_repairs: Callable[..., Awaitable[int]]


def _parse_arguments(tool_name: str, arguments_str: str) -> dict[str, Any]:
    # 参数解析三级容错
    args = parse_as_dict(arguments_str) if arguments_str else {}

    # 空参数检查：对需要参数的工具做提示
    if not args and arguments_str and arguments_str.strip() not in ("{}", ""):
        logger.warning(
            "tool args parse failed, using empty dict | tool=%s raw=%s",
            tool_name,
            arguments_str[:200],
        )
        # 尝试用正则提取关键参数
        if tool_name in ("search_photos", "fallback_search"):
            query_regex = extract_json_field_by_regex(arguments_str, "query", "")
            if query_regex:
                args = {"query": query_regex}
        elif tool_name == "get_photo_detail":
            pid_regex = extract_json_field_by_regex(arguments_str, "photo_id", "")
            if pid_regex:
                args = {"photo_id": pid_regex}
        elif tool_name == "apply_skill":
            pid_regex = extract_json_field_by_regex(arguments_str, "photo_id", "")
            sid_regex = extract_json_field_by_regex(arguments_str, "skill_id", "")
            prompt_regex = extract_json_field_by_regex(arguments_str, "prompt", "")
            args = {
                k: v
                for k, v in {
                    "photo_id": pid_regex,
                    "skill_id": sid_regex,
                    "prompt": prompt_regex,
                }.items()
                if v
            }
    return args


def _prepare_arguments(
    agent: Any,
    user_id: UUID,
    tool_name: str,
    args: dict[str, Any],
    state: AgentState,
) -> tuple[dict[str, Any], dict | None]:
    # 注入公共参数
    args.setdefault("user_id", user_id)
    args.setdefault("db", agent.db)

    # “还有一张/再来一张”等续搜由代码继承语义与排除集合，不能依赖模型猜测。
    if state.followup_type == "more_search_results":
        active_query = str(state.active_search.get("resolved_query", "")).strip()
        excluded = {
            str(value)
            for value in (
                list(state.active_search.get("shown_photo_ids", []))
                + list(state.rejected_photo_ids)
            )
            if value
        }
        if tool_name in {"search_photos", "fallback_search"} and active_query:
            args["query"] = active_query
            args["exclude_photo_ids"] = sorted(excluded)
            args["result_mode"] = "browse"
            if tool_name == "fallback_search":
                args.pop("result_mode", None)
                args["allow_unfiltered_browse"] = False
        elif tool_name == "browse_candidates":
            return args, {
                "ok": False,
                "hint": "当前是上一轮搜索的续搜，不能退化为无条件浏览全相册；"
                "请继续使用原查询搜索并排除已展示照片。",
            }

    # 类型转换：LLM 返回的 UUID 字段是字符串，转为 UUID 对象
    for uuid_field in ("photo_id", "skill_id"):
        val = args.get(uuid_field)
        if val is not None and not isinstance(val, UUID):
            try:
                args[uuid_field] = UUID(str(val))
            except (ValueError, AttributeError):
                return args, {"ok": False, "error": f"无效的 {uuid_field}: {val}"}

    # 路由层通过 JSON 传递 ISO 日期；工具签名需要 date 对象。
    for date_field in ("from_date", "to_date"):
        val = args.get(date_field)
        if val is not None and not isinstance(val, date):
            try:
                args[date_field] = date.fromisoformat(str(val))
            except (TypeError, ValueError):
                return args, {"ok": False, "error": f"无效的 {date_field}: {val}"}
    return args, None


def _apply_tool_policies(
    agent: Any,
    tool_name: str,
    args: dict[str, Any],
    state: AgentState,
) -> dict | None:
    # 特殊业务逻辑：search_photos 次数限制
    if tool_name == "search_photos":
        transition_workflow(state, "searching")
        user_selection_limit = _requested_user_selection_limit(state.original_query)
        complete_result_set = _requests_complete_result_set(state.original_query)
        if complete_result_set:
            args["result_mode"] = "select"
            args["complete_result_set"] = True
        elif user_selection_limit is not None:
            args["result_mode"] = "select"
            args["limit"] = user_selection_limit
        if state.search_attempts >= agent.constraints.max_searches:
            return {
                "ok": False,
                "hint": f"已达到最大搜索次数（{agent.constraints.max_searches}），"
                "建议调用 fallback_search 兜底或向用户确认需求。",
            }
        state.search_attempts += 1
        args.setdefault("include_index_coverage", True)

    # 特殊业务逻辑：ask_clarification 次数限制
    if tool_name == "ask_clarification":
        if state.followup_type == "more_search_results":
            return {
                "ok": False,
                "hint": "短期记忆中已有可继承的搜索目标，不要澄清；"
                "请继续原搜索并排除已展示照片。",
            }
        if state.search_attempts > 0:
            return {
                "ok": False,
                "hint": "已经执行过普通搜索，不再因搜索失败向用户澄清；"
                "请调用 fallback_search 兜底。",
            }
        if state.clarification_attempts >= agent.constraints.max_clarifications:
            return {
                "ok": False,
                "hint": f"已达到最大澄清次数（{agent.constraints.max_clarifications}），"
                "请直接根据现有信息给出最佳结果。",
            }
        state.clarification_attempts += 1

    # v2 不允许模型直接替用户确定照片；控制组继续兼容旧流程。
    if tool_name == "apply_skill":
        selection_mode = (
            state.active_search.get("filters", {}).get("result_mode") == "select"
        )
        if (
            state.agent_variant == "v2" or selection_mode
        ) and not state.confirmed_photo_id:
            return {
                "ok": False,
                "error_type": "confirmation_required",
                "hint": "这些照片需要由用户本人选择；请等待用户点击“选择这张”，不能替用户决定。",
            }
        if state.confirmed_photo_id and args.get("photo_id"):
            if str(args["photo_id"]) != str(state.confirmed_photo_id):
                return {
                    "ok": False,
                    "error_type": "selected_photo_mismatch",
                    "hint": "工具指定的照片不是用户刚刚确认的照片，请使用 confirmed_photo_id。",
                }
        if not state.confirmed_photo_id and not state.last_search_items:
            return {
                "ok": False,
                "error_type": "confirmation_required",
                "hint": "请先搜索或浏览照片，选中一张后再进行 AI 改造。",
            }
        # P0-1: 会话级费用预算检查
        if state.total_cost >= agent.constraints.max_cost_yuan:
            return {
                "ok": False,
                "error_type": "cost_budget_exceeded",
                "hint": f"本会话生成费用已达上限（{agent.constraints.max_cost_yuan}元），"
                "请开启新会话。",
            }
        if state.agent_variant == "v2":
            raw_key = "|".join(
                (
                    str(state.session_id),
                    str(args.get("photo_id", "")),
                    str(args.get("skill_id", "")),
                    str(args.get("extra_prompt", "")),
                    str(args.get("model", "")),
                )
            )
            args["idempotency_key"] = hashlib.sha256(
                raw_key.encode("utf-8")
            ).hexdigest()
            args["require_confirmation"] = True
        else:
            args["require_confirmation"] = False
    return None


async def _invoke_registered_tool(
    agent: Any,
    spec: Any,
    tool_name: str,
    args: dict[str, Any],
    state: AgentState,
) -> tuple[dict, bool]:
    # P0-2: 工具执行超时保护
    tool_timeout = spec.timeout or agent.constraints.tool_timeout
    if tool_name == "search_photos" and args.get("complete_result_set"):
        tool_timeout = max(float(tool_timeout), 60.0)
    if tool_name == "search_photos" and state.followup_type == "more_search_results":
        tool_timeout = min(
            float(tool_timeout), settings.agent_search_turn_budget_seconds
        )
    try:
        with start_span(
            f"execute_tool {tool_name}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool_name,
                "tool.timeout_seconds": float(tool_timeout),
            },
        ):
            result = await asyncio.wait_for(spec.fn(**args), timeout=tool_timeout)
            set_current_span_attributes(
                {
                    "tool.ok": bool(result.get("ok", True)),
                    "tool.result_count": len(result.get("items", []) or []),
                    "tool.error_type": result.get("error_type"),
                }
            )
    except TimeoutError:
        logger.warning(
            "Tool execution timed out | tool=%s timeout=%.1fs",
            tool_name,
            tool_timeout,
        )
        if (
            tool_name == "search_photos"
            and state.followup_type == "more_search_results"
        ):
            return (
                {
                    "ok": True,
                    "items": [],
                    "total": 0,
                    "search_pending": True,
                    "error_type": "timeout_resumable",
                    "hint": "本轮时间预算已用完，搜索进度已保留",
                },
                False,
            )
        return (
            {
                "ok": False,
                "error_type": "timeout",
                "error": f"工具 {tool_name} 执行超时（{tool_timeout}s）",
            },
            False,
        )
    except Exception as exc:
        logger.exception("Tool execution failed | tool=%s", tool_name)
        return (
            {
                "ok": False,
                "error_type": _classify_exception(exc),
                "error": str(exc),
            },
            False,
        )
    return result, True


async def _run_search_maintenance(
    agent: Any,
    dependencies: AgentExecutionDependencies,
    user_id: UUID,
    tool_name: str,
    args: dict[str, Any],
    result: dict,
    state: AgentState,
) -> tuple[list[dict], str | None, str | None]:
    candidate_pool_items = list(result.pop("_candidate_pool_items", []) or [])

    # 首次搜索后只投递后台预取任务，不再在用户请求内同步执行第二次
    # 多批判同。这样首轮响应与后续“还有一张”不会争用同一个 15 秒预算。
    prefetch_status: str | None = None
    prefetch_pool_key: str | None = None
    if (
        tool_name == "search_photos"
        and state.followup_type != "more_search_results"
        and result.get("items")
        and (result.get("rerank_check") or {}).get("applied")
        and not (result.get("rerank_check") or {}).get("degraded")
        and settings.agent_search_candidate_pool_size > 0
    ):
        shown = {
            str(item.get("id"))
            for item in result.get("items", [])
            if isinstance(item, dict) and item.get("id")
        }
        excluded = sorted(
            shown | {str(value) for value in state.rejected_photo_ids if value}
        )
        query_used = str(args.get("query", state.original_query)).strip()
        prefetch_pool_key = dependencies.candidate_pool_key(state.session_id)
        try:
            from app.workers.search_tasks import enqueue_search_prefetch

            queued = await asyncio.wait_for(
                enqueue_search_prefetch(
                    session_id=str(state.session_id),
                    user_id=str(user_id),
                    query=query_used,
                    exclude_photo_ids=excluded,
                ),
                timeout=max(0.5, settings.agent_search_prefetch_wait_seconds),
            )
            prefetch_status = "queued" if queued else "failed"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Agent candidate prefetch enqueue degraded | error=%s: %s",
                type(exc).__name__,
                exc,
            )
            prefetch_status = "failed"

    if (
        tool_name == "search_photos"
        and state.followup_type == "more_search_results"
        and result.get("ok")
        and not result.get("items")
        and not _search_coverage_complete(
            result.get("index_coverage"),
            requires_semantic_facets=bool(result.get("semantic_facets_required")),
        )
        and not result.get("semantic_facets_required")
        and settings.agent_search_auto_repair_index
    ):
        try:
            result["index_repair_queued"] = await dependencies.enqueue_index_repairs(
                agent.db,
                user_id,
                limit=settings.agent_search_index_repair_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Agent index repair enqueue degraded | error=%s: %s",
                type(exc).__name__,
                exc,
            )
            result["index_repair_queued"] = 0
    return candidate_pool_items, prefetch_status, prefetch_pool_key


def _apply_result_to_state(
    tool_name: str,
    args: dict[str, Any],
    result: dict,
    state: AgentState,
    candidate_pool_items: list[dict],
    prefetch_status: str | None,
    prefetch_pool_key: str | None,
) -> None:
    # 更新 State
    if tool_name in ("search_photos", "fallback_search") and result.get("ok"):
        items = result.get("items", [])
        if args.get("result_mode") == "select":
            # 新的用户自选列表尚未产生选择，不能沿用上一轮确认的照片。
            state.confirmed_photo_id = None
        is_continuation = state.followup_type == "more_search_results"
        shown_ids = (
            list(state.active_search.get("shown_photo_ids", []))
            if is_continuation
            else []
        )
        for item in items:
            photo_id = str(item.get("id", "")) if isinstance(item, dict) else ""
            if photo_id and photo_id not in shown_ids:
                shown_ids.append(photo_id)
        query_used = str(args.get("query", state.original_query)).strip()
        existing_pool = (
            list(state.active_search.get("candidate_pool_items", []))
            if is_continuation
            else []
        )
        pool_by_id: dict[str, dict] = {}
        for candidate in [*existing_pool, *candidate_pool_items]:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("id", ""))
            if candidate_id and candidate_id not in shown_ids:
                pool_by_id.setdefault(candidate_id, candidate)
        candidate_pool = list(pool_by_id.values())[
            : settings.agent_search_candidate_pool_size
        ]
        coverage = result.get("index_coverage") or state.active_search.get(
            "index_coverage"
        )
        semantic_facets_required = bool(
            result.get("semantic_facets_required")
            or (is_continuation and state.active_search.get("semantic_facets_required"))
        )
        coverage_complete = _search_coverage_complete(
            coverage,
            requires_semantic_facets=semantic_facets_required,
        )
        state.last_search_items = (
            items[:30]
            if args.get("result_mode") == "select" and len(items) > 30
            else items
        )
        state.active_intent = "search_photos"
        state.active_search = {
            "raw_query": (
                state.active_search.get("raw_query", state.original_query)
                if is_continuation
                else state.original_query
            ),
            "resolved_query": query_used,
            "filters": {
                key: args.get(key)
                for key in (
                    "from_date",
                    "to_date",
                    "result_mode",
                    "complete_result_set",
                )
                if args.get(key) is not None
            },
            "shown_photo_ids": shown_ids,
            "rejected_photo_ids": sorted(state.rejected_photo_ids),
            "candidate_pool_items": candidate_pool,
            "candidate_pool_count": len(candidate_pool),
            "pool_key": (
                prefetch_pool_key
                if prefetch_pool_key is not None
                else state.active_search.get("pool_key")
            ),
            "prefetch_status": (
                prefetch_status
                if prefetch_status is not None
                else state.active_search.get("prefetch_status")
            ),
            "recall_stage": (
                int(state.active_search.get("recall_stage", 0)) + 1
                if is_continuation
                else 0
            ),
            "index_coverage": coverage,
            "semantic_facets_required": semantic_facets_required,
            "next_cursor": result.get("next_cursor"),
            "exhausted": bool(result.get("search_exhausted"))
            or bool(result.get("result_set_complete"))
            or (
                is_continuation
                and not items
                and not candidate_pool
                and not result.get("search_pending")
                and coverage_complete
            ),
        }
        state.fallback_level = result.get("fallback_level", state.fallback_level)
        target_state = (
            "awaiting_selection"
            if args.get("result_mode") == "select"
            else "results_ready"
        )
        transition_workflow(state, target_state)
        metrics.record_search_result(
            variant=state.agent_variant,
            result_count=len(items),
            complete=bool(result.get("result_set_complete")),
            degraded=bool(result.get("degraded") or result.get("error_type")),
        )
    elif tool_name == "apply_skill" and result.get("ok"):
        state.confirmed_generation_id = result.get("generation_id")
        if result.get("confirmation_required"):
            transition_workflow(state, "awaiting_generation_confirmation")
        else:
            transition_workflow(state, "generation_queued")
            state.total_cost += float(result.get("estimated_cost_yuan") or 0)


async def execute_tool(
    agent: Any,
    dependencies: AgentExecutionDependencies,
    user_id: UUID,
    tool_name: str,
    arguments_str: str,
    state: AgentState,
) -> dict:
    """Dispatch a tool through parsing, policy, invocation, and state updates."""
    if tool_name == "final_answer":
        args = parse_json_or_default(arguments_str, default=None)
        if isinstance(args, dict) and args.get("message"):
            return {"ok": True, "message": str(args.get("message", ""))}
        msg_by_regex = extract_json_field_by_regex(arguments_str or "", "message", "")
        if msg_by_regex:
            return {"ok": True, "message": msg_by_regex}
        return {
            "ok": True,
            "message": str(arguments_str or "好的，已为你找到相关照片。"),
        }

    spec = agent.registry.get(tool_name)
    if spec is None:
        logger.warning("unknown tool called: %s", tool_name)
        return {"ok": False, "error": f"未知工具：{tool_name}"}

    args = _parse_arguments(tool_name, arguments_str)
    args, error = _prepare_arguments(agent, user_id, tool_name, args, state)
    if error is not None:
        return error
    error = _apply_tool_policies(agent, tool_name, args, state)
    if error is not None:
        return error

    result, invoked = await _invoke_registered_tool(agent, spec, tool_name, args, state)
    if not invoked:
        return result
    (
        candidate_pool_items,
        prefetch_status,
        prefetch_pool_key,
    ) = await _run_search_maintenance(
        agent, dependencies, user_id, tool_name, args, result, state
    )
    _apply_result_to_state(
        tool_name,
        args,
        result,
        state,
        candidate_pool_items,
        prefetch_status,
        prefetch_pool_key,
    )
    return result
