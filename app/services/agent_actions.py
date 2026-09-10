"""Model-selected actions; state transitions and pagination remain server-owned."""

from __future__ import annotations

from copy import deepcopy
import asyncio
import json

from app.services.search_feedback import new_goal, record_batch, accept_results
from app.services.agent_workflow import transition_workflow

ACTION_TOOLS = frozenset(
    {
        "search_photos",
        "continue_search",
        "feedback_results",
        "browse_album",
        "undo_feedback",
    }
)


def configure_action_tools(registry, spec_type):
    finish = {
        "type": "boolean",
        "default": True,
        "description": "展示结果后结束本轮；还有后续工具工作才设false",
    }
    limit = {"type": "integer", "minimum": 1, "maximum": 1000000}
    search = registry.get("search_photos")
    props = search.parameters["properties"]
    props.pop("exclude_photo_ids", None)
    props.update(
        change={"type": "string", "enum": ["new", "refine"]}, finish_turn=finish
    )
    # These filters already exist on the business search tool.
    for key in ("scene", "mood"):
        props[key] = {"type": "string"}
    for key in ("tags", "objects", "text_in_image", "colors"):
        props[key] = {"type": "array", "items": {"type": "string"}}
    search.parameters.update(required=["query", "change"], additionalProperties=False)
    props["limit"] = limit
    definitions = {
        "undo_feedback": (
            "撤销最近一次反馈；搜索或翻页后失效。",
            {"undo_id": {"type": "string"}},
            ["undo_id"],
        ),
        "continue_search": (
            "继续当前搜索或浏览，继承条件并排除已展示照片。",
            {"limit": limit, "finish_turn": finish},
            [],
        ),
        "browse_album": (
            "用户明确要求浏览全相册时使用。",
            {"limit": limit, "finish_turn": finish},
            [],
        ),
        "feedback_results": (
            "只记录拒绝或满意，不执行搜索。仅反馈时finish_turn=true。明确要求再找时finish_turn=false，随后调用continue_search。整批拒绝用reject_batch。",
            {
                "kind": {
                    "type": "string",
                    "enum": ["reject_items", "reject_batch", "satisfied"],
                },
                "photo_ids": {"type": "array", "items": {"type": "string"}},
                "batch_id": {"type": "string"},
                "finish_turn": finish,
            },
            ["kind"],
        ),
    }
    for name, (description, properties, required) in definitions.items():
        registry.register(
            spec_type(
                name,
                description,
                {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                _unreachable,
            )
        )


async def _unreachable(**kwargs):
    raise RuntimeError("action must use the state-aware executor")


def _valid(value, schema):
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
    }
    if kind in types and not isinstance(value, types[kind]):
        return False
    if kind in {"integer", "number"} and isinstance(value, bool):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if kind == "object":
        props = schema.get("properties", {})
        return (
            all(k in value for k in schema.get("required", []))
            and (schema.get("additionalProperties", True) or set(value) <= set(props))
            and all(_valid(v, props[k]) for k, v in value.items() if k in props)
        )
    if kind == "array":
        return all(_valid(v, schema.get("items", {})) for v in value)
    if kind in {"integer", "number"}:
        return (
            schema.get("minimum", float("-inf"))
            <= value
            <= schema.get("maximum", float("inf"))
        )
    return True


def _emit(state, kind, payload):
    if state.emit_event:
        state.emit_event(kind, payload)


def _reset(state, change):
    state.feedback_undo = None
    if change == "new" or not state.search_feedback.get("goal_id"):
        new_goal(state)
        state.rejected_photo_ids.clear()
        state.result_batches.clear()
    state.active_search = {}
    state.last_search_items = []
    state.confirmed_photo_id = None
    state.confirmed_generation_id = None
    state.pending_clarification = None
    state.search_feedback.pop("pending_expansion", None)
    state.search_feedback["scope"] = "matches"
    state.search_feedback["batch_id"] = None
    state.search_feedback["batch_photo_ids"] = []
    state.fallback_level = 0
    _emit(
        state,
        "search_state",
        {"search_goal_id": state.search_feedback["goal_id"], "display_mode": "replace"},
    )


def _publish(state, result, mode):
    if not result.get("ok"):
        return result
    result["display_mode"] = mode
    if "items" in result:
        record_batch(state, result)
        if result.get("items"):
            batch = {
                "batch_id": result["result_batch_id"],
                "photo_ids": [str(p["id"]) for p in result["items"]],
                "goal_id": state.search_feedback["goal_id"],
                "number": max(
                    (b.get("number", 0) for b in state.result_batches), default=0
                )
                + 1,
            }
            if not any(
                b["batch_id"] == batch["batch_id"] for b in state.result_batches
            ):
                state.result_batches.append(batch)
            state.result_batches = state.result_batches[-64:]
            saved_batch = next(
                b for b in state.result_batches if b["batch_id"] == batch["batch_id"]
            )
            for position, item in enumerate(result["items"], 1):
                item.update(
                    result_batch_id=saved_batch["batch_id"],
                    batch_number=saved_batch.get("number", 1),
                    batch_position=position,
                )
            saved_batch["items"] = deepcopy(result["items"])
            result["result_batch_number"] = saved_batch.get("number", 1)
    result["search_goal_id"] = state.search_feedback.get("goal_id")
    result["selected_photo_id"] = state.confirmed_photo_id
    scope = state.search_feedback.get("scope", "matches")
    result["browse_scope"] = scope
    if "message" not in result:
        count = len(result.get("items", []))
        result["message"] = (
            f"已展示 {count} 张照片。"
            if count
            else "本轮没有新的照片；请根据搜索范围和索引进度继续查找。"
        )
    if scope != "matches":
        result["message"] += (
            "这些是浏览候选，未经搜索相关性核验，并非已确认匹配原搜索条件。"
        )
    return result


async def execute_action(agent, dependencies, user_id, name, arguments, state):
    from app.services.agent_execution import execute_registered_tool

    check = agent.db.info.get("ownership_check")
    if check:
        await check()
    try:
        args = json.loads(arguments or "{}")
    except (ValueError, TypeError):
        args = None
    spec = agent.registry.get(name)
    if spec is None or not _valid(args, spec.parameters):
        return {
            "ok": False,
            "error_type": "invalid_arguments",
            "hint": "参数不符合工具契约，请修正。",
        }
    args = dict(args)
    finish = args.pop("finish_turn", True)

    async def invoke(tool, values, continuation=False):
        if tool in {"fallback_search", "browse_candidates"}:
            if state.search_attempts >= agent.constraints.max_searches:
                return {
                    "ok": False,
                    "error_type": "search_budget",
                    "hint": "本轮搜索次数已达上限。",
                }
            state.search_attempts += 1
            transition_workflow(state, "searching")
        previous = state.search_action
        state.search_action = "continue" if continuation else "search"
        try:
            return await execute_registered_tool(
                agent,
                dependencies,
                user_id,
                tool,
                json.dumps(values, default=str),
                state,
            )
        finally:
            state.search_action = previous

    async def more(limit=None, level=None):
        active = state.active_search
        if not active.get("resolved_query"):
            return {
                "ok": False,
                "error_type": "missing_search",
                "hint": "没有可继续的搜索，请先明确搜索目标。",
            }
        scope = state.search_feedback.get("scope", "matches")
        if scope == "clues":
            return {
                "ok": False,
                "error_type": "scope_requires_user",
                "hint": "旧线索浏览已停止，请重新搜索或明确要求浏览相册。",
            }
        if scope == "matches" and any(
            item.get("verification_status")
            in {"unverified", "uncertain", "contradiction"}
            for item in state.last_search_items
        ):
            return {
                "ok": False,
                "error_type": "search_verification_policy_changed",
                "hint": "旧结果未经过严格核验，请重新搜索。",
            }
        if scope != "matches" and not active.get("next_cursor") and level is None:
            return {"ok": True, "items": [], "message": "当前浏览范围已到最后一页。"}
        if not active.get("plan_id") and scope == "matches":
            return {
                "ok": False,
                "error_type": "missing_search_plan",
                "hint": "旧搜索缺少分页依据，请重新搜索。",
            }
        excluded = sorted(
            set(active.get("shown_photo_ids", [])) | state.rejected_photo_ids
        )
        selected = state.confirmed_photo_id
        saved = deepcopy(active)
        pool_scope = active.get("pool_scope")
        if (
            scope == "matches"
            and level is None
            and pool_scope
            and active.get("prefetch_status")
        ):
            from app.services import search_candidate_pool as pool

            popped = []
            try:
                async with asyncio.timeout(2):
                    status = await pool.get_prefetch_status(pool_scope)
                    for _ in range(
                        min(limit or active.get("filters", {}).get("limit", 5), 30)
                    ):
                        item = await pool.pop_verified_candidate(pool_scope)
                        if item is None:
                            break
                        popped.append(item)
                if check:
                    await check()
            except BaseException:
                if popped:
                    await pool.push_verified_candidates(pool_scope, popped)
                raise
            active["candidate_pool_items"] = (
                active.get("candidate_pool_items", []) + popped
            )
            if not active["candidate_pool_items"] and status in {"queued", "running"}:
                return {
                    "ok": True,
                    "items": [],
                    "search_pending": True,
                    "message": "后台仍在筛选，已保留当前结果和搜索进度。",
                }
        if scope == "matches" and level is None and active.get("candidate_pool_items"):
            # Recheck ownership/version of local verified candidates before display.
            from app.services.agent_runtime import _refresh_candidate

            pending = list(active["candidate_pool_items"])
            fresh = []
            while pending and len(fresh) < (
                limit or active.get("filters", {}).get("limit", 5)
            ):
                item = await _refresh_candidate(
                    agent.db, user_id, pending.pop(0), excluded
                )
                if item is not None and item["id"] not in {p["id"] for p in fresh}:
                    fresh.append(item)
            if fresh:
                if check:
                    await check()
                active["candidate_pool_items"] = pending
                active["candidate_pool_count"] = len(pending)
                active["shown_photo_ids"] = list(
                    dict.fromkeys(
                        active.get("shown_photo_ids", []) + [p["id"] for p in fresh]
                    )
                )
                state.last_search_items = fresh
                return {
                    "ok": True,
                    "items": fresh,
                    "source": "candidate_pool",
                    "next_cursor": active.get("next_cursor"),
                }
        if scope == "all":
            values = {
                "limit": limit or 30,
                "cursor": active.get("next_cursor"),
                "exclude_photo_ids": excluded,
            }
            result = await invoke("browse_candidates", values, True)
        elif level is not None or scope == "clues":
            values = {
                "query": active["resolved_query"],
                "feedback_level": level or 2,
                "cursor": None if level is not None else active.get("next_cursor"),
                "limit": limit or 30,
                "exclude_photo_ids": excluded,
            }
            result = await invoke("fallback_search", values, True)
        else:
            values = {
                **active.get("filters", {}),
                "query": active["resolved_query"],
                "exclude_photo_ids": excluded,
            }
            if limit is not None:
                values["limit"] = limit
            result = await invoke("search_photos", values, True)
        if not result.get("ok"):
            state.active_search = saved
            state.confirmed_photo_id = selected
            return result
        result["items"] = [
            p for p in result.get("items", []) if str(p.get("id")) not in excluded
        ]
        state.last_search_items = result["items"]
        state.active_search["shown_photo_ids"] = list(
            dict.fromkeys(
                saved.get("shown_photo_ids", [])
                + [str(p["id"]) for p in result["items"]]
            )
        )
        state.active_search["next_cursor"] = result.get("next_cursor")
        state.active_search["filters"] = saved.get("filters", {})
        state.search_feedback["scope"] = result.get("browse_scope", scope)
        state.confirmed_photo_id = selected
        if selected:
            transition_workflow(state, "selection_confirmed")
        return result

    if name == "undo_feedback":
        from app.services.agent_feedback_undo import undo_feedback

        return await undo_feedback(agent, user_id, state, args["undo_id"])
    mode = "append"
    if name == "search_photos":
        if not args.get("query", "").strip():
            return {
                "ok": False,
                "error_type": "invalid_arguments",
                "hint": "搜索内容不能为空。",
            }
        change = args.pop("change")
        # Parse dates before clearing the current goal.
        from datetime import date

        try:
            for key in ("from_date", "to_date"):
                if key in args:
                    date.fromisoformat(args[key])
        except ValueError:
            return {
                "ok": False,
                "error_type": "invalid_arguments",
                "hint": "日期必须为 YYYY-MM-DD。",
            }
        _reset(state, change)
        args["exclude_photo_ids"] = sorted(state.rejected_photo_ids)
        args.setdefault("limit", 5)
        # The decision model already resolved conditions; do not reparse its query.
        args["auto_parse"] = False
        state.active_search = {
            "resolved_query": args["query"],
            "filters": {
                k: v for k, v in args.items() if k not in {"query", "exclude_photo_ids"}
            },
        }
        result = await invoke("search_photos", args)
        mode = "replace"
    elif name == "continue_search":
        state.feedback_undo = None
        _emit(state, "undo_available", {"undo_id": None})
        level = state.search_feedback.get("pending_expansion")
        result = await more(args.get("limit"), level=level)
        if result.get("ok"):
            state.search_feedback.pop("pending_expansion", None)
    elif name == "browse_album":
        _reset(state, "new")
        state.search_feedback["scope"] = "all"
        state.active_search = {"resolved_query": "全部相册浏览", "filters": {}}
        transition_workflow(state, "searching")
        result = await invoke("browse_candidates", {"limit": args.get("limit", 30)})
        if result.get("ok"):
            state.last_search_items = result.get("items", [])
            state.active_search.update(
                next_cursor=result.get("next_cursor"),
                shown_photo_ids=[str(p["id"]) for p in state.last_search_items],
            )
            transition_workflow(state, "results_ready")
        mode = "replace"
    else:
        if not state.search_feedback.get("batch_id") and state.last_search_items:
            _publish(state, {"ok": True, "items": state.last_search_items}, "append")
        batch_id = (
            args.get("batch_id")
            or state.feedback_batch_id
            or state.search_feedback.get("batch_id")
        )
        batches = {
            b["batch_id"]: b
            for b in state.result_batches
            if b.get("goal_id") == state.search_feedback.get("goal_id")
        }
        batch = batches.get(batch_id)
        if not batch:
            return {
                "ok": False,
                "error_type": "stale_feedback",
                "hint": "无法定位这批照片，请基于当前照片重新反馈。",
            }
        kind = args["kind"]
        from app.services.agent_feedback_undo import capture_feedback

        undo = capture_feedback(state, batch)

        removed = set()
        if kind == "satisfied":
            accept_results(state)
        elif kind == "reject_items":
            removed = set(args.get("photo_ids", []))
            if not removed or not removed <= set(batch["photo_ids"]):
                return {
                    "ok": False,
                    "error_type": "invalid_photo_reference",
                    "hint": "照片指代不明确，请确认照片及批次。",
                }
        else:
            counted = state.search_feedback.setdefault("counted_batches", [])
            if batch_id in counted:
                return {
                    "ok": True,
                    "message": "这批反馈已记录，不会重复扩大范围。",
                    "finish_turn": finish,
                }
            if len(counted) >= 64:
                return {
                    "ok": False,
                    "error_type": "feedback_limit",
                    "hint": "反馈记录已满，请开始新的搜索。",
                }
            counted.append(batch_id)
            state.search_feedback["rejected_batch_count"] = min(
                2, state.search_feedback.get("rejected_batch_count", 0) + 1
            )
            removed = set(batch["photo_ids"])
        newly_removed = removed - state.rejected_photo_ids
        if removed and not newly_removed:
            return {"ok": True, "finish_turn": finish, "message": "这张照片已移除。"}
        _apply_result_feedback_to_state(state, sorted(removed))
        state.feedback_undo = (
            dict(undo, removed=sorted(newly_removed)) if newly_removed else None
        )
        if kind == "reject_batch" and state.search_feedback.get("scope") == "matches":
            state.search_feedback["pending_expansion"] = state.search_feedback.get(
                "rejected_batch_count", 0
            )
        if kind == "satisfied":
            state.search_feedback.pop("pending_expansion", None)
        _emit(
            state,
            "feedback",
            {
                "removed_photo_ids": sorted(removed),
                "result_batch_id": batch_id,
                "continue_search": False,
                "undo_id": state.feedback_undo["undo_id"]
                if state.feedback_undo
                else None,
            },
        )
        result = {
            "ok": True,
            "message": f"已移除 {len(removed)} 张照片。"
            if removed
            else "已记下你的反馈。",
            "removed_photo_ids": sorted(removed),
        }
    if result.get("ok"):
        _publish(state, result, mode)
        result["finish_turn"] = finish or (
            name != "feedback_results" and state.workflow_state == "awaiting_selection"
        )
    return result


def _apply_result_feedback_to_state(
    state,
    photo_ids: list[str],
) -> list[str]:
    """Apply only feedback IDs that belong to the current trusted result state."""

    visible_ids = {
        str(item.get("id"))
        for item in state.last_search_items
        if isinstance(item, dict) and item.get("id")
    }
    known_ids = set(visible_ids)
    known_ids.update(
        str(value) for value in state.active_search.get("shown_photo_ids", []) if value
    )
    for batch in state.result_batches:
        if batch.get("goal_id") == state.search_feedback.get("goal_id"):
            known_ids.update(batch.get("photo_ids", []))
    if state.confirmed_photo_id:
        known_ids.add(str(state.confirmed_photo_id))
    applied = sorted({str(value) for value in photo_ids if str(value) in known_ids})
    if not applied:
        return []

    rejected = set(applied)
    state.rejected_photo_ids.update(rejected)
    state.last_search_items = [
        item
        for item in state.last_search_items
        if not isinstance(item, dict) or str(item.get("id", "")) not in rejected
    ]
    candidate_pool = [
        item
        for item in state.active_search.get("candidate_pool_items", [])
        if not isinstance(item, dict) or str(item.get("id", "")) not in rejected
    ]
    state.active_search["candidate_pool_items"] = candidate_pool
    state.active_search["candidate_pool_count"] = len(candidate_pool)
    state.active_search["rejected_photo_ids"] = sorted(state.rejected_photo_ids)
    shown_ids = {
        str(value) for value in state.active_search.get("shown_photo_ids", []) if value
    }
    state.active_search["shown_photo_ids"] = sorted(shown_ids | visible_ids)
    if state.confirmed_photo_id in rejected:
        state.confirmed_photo_id = None
    return applied
