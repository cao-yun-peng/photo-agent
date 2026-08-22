"""Deterministic conversation memory and result-message helpers."""

from __future__ import annotations

import json
from typing import Any

from app.services.agent_state import AgentState

_RECENT_MESSAGE_LIMIT = 10
_CONVERSATION_SUMMARY_CHAR_LIMIT = 2000


def _remember_message(state: AgentState, role: str, content: str) -> None:
    """保存干净的自然语言消息，并把溢出的旧消息压缩到确定性摘要。"""
    text = str(content or "").strip()
    if not text:
        return
    state.recent_messages.append({"role": role, "content": text[:1000]})
    overflow = max(0, len(state.recent_messages) - _RECENT_MESSAGE_LIMIT)
    if not overflow:
        return
    archived = state.recent_messages[:overflow]
    state.recent_messages = state.recent_messages[overflow:]
    lines = [
        f"{('用户' if item.get('role') == 'user' else '助手')}：{item.get('content', '')}"
        for item in archived
        if item.get("content")
    ]
    combined = "\n".join(part for part in (state.conversation_summary, *lines) if part)
    state.conversation_summary = combined[-_CONVERSATION_SUMMARY_CHAR_LIMIT:]


def _search_result_fallback_message(result_count: int) -> str:
    """搜索已成功、模型文案失败时生成不依赖外部服务的完成文案。"""

    return (
        f"已找到 {result_count} 张符合条件的照片，结果已展示。"
        "智能说明暂时不可用，你可以选择照片查看详情，"
        "或说“还有一张”继续搜索。"
    )


def _search_coverage_complete(
    coverage: dict[str, Any] | None,
    *,
    requires_semantic_facets: bool = False,
) -> bool:
    coverage = coverage or {}
    return bool(
        coverage.get("complete", True)
        and (
            coverage.get("semantic_complete", True)
            if requires_semantic_facets
            else True
        )
    )


def _fast_search_message(result: dict[str, Any]) -> str:
    """普通搜索快路径的本地文案，不为一句结果说明再次调用模型。"""

    if result.get("ok") and result.get("items"):
        if result.get("result_mode") == "select":
            count = int(result.get("total", len(result["items"])) or 0)
            if result.get("result_set_complete"):
                message = f"已完整加载 {count} 张匹配照片，请由你本人选择。"
            else:
                message = f"已加载 {count} 张匹配照片，请由你本人选择。"
        else:
            message = f"找到 {len(result['items'])} 张符合条件的照片，已为你展示。"
        if result.get("coverage_hint"):
            message += str(result["coverage_hint"])
        return message
    if result.get("ok"):
        coverage = result.get("index_coverage") or {}
        if not _search_coverage_complete(
            coverage,
            requires_semantic_facets=bool(result.get("semantic_facets_required")),
        ):
            return "当前还没有找到符合条件的照片；相册索引尚未完整，稍后可以再试。"
        return "暂时没有找到符合条件的照片。你可以换一个更宽泛的描述。"
    if result.get("error_type") == "timeout":
        return "搜索照片超时了，请稍后再试。"
    return "搜索照片时出现问题，请稍后再试。"


def _model_tool_content(tool_name: str, result: dict[str, Any]) -> str:
    """为下一次模型决策构造紧凑且始终合法的 JSON。

    前端仍接收完整工具结果；模型只获得决策所需字段，避免签名 URL、完整
    图片分析和内部候选池挤占上下文。不能对 JSON 字符串做字符级截断，否则
    会破坏 Function Calling 消息协议。
    """

    scalar_keys = (
        "ok",
        "error",
        "error_type",
        "hint",
        "message",
        "needs_clarification",
        "question",
        "total",
        "result_mode",
        "selection_owner",
        "complete_result_set",
        "result_set_complete",
        "completeness_reason",
        "total_matches",
        "truncated",
        "similarity_threshold",
        "threshold_filtered_count",
        "threshold_bypassed_reason",
        "coverage_hint",
        "semantic_facets_required",
        "search_pending",
        "search_exhausted",
        "index_repair_queued",
        "generation_id",
        "status",
    )
    compact: dict[str, Any] = {key: result[key] for key in scalar_keys if key in result}
    if isinstance(result.get("options"), list):
        compact["options"] = [str(value)[:120] for value in result["options"][:8]]

    if tool_name in {"search_photos", "fallback_search", "browse_candidates"}:
        compact_items = []
        for position, item in enumerate(result.get("items") or [], start=1):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            compact_items.append(
                {
                    "position": position,
                    "id": str(item["id"]),
                    "description": str(item.get("ai_description") or "")[:160],
                    "taken_at": item.get("taken_at"),
                    "status": item.get("status"),
                    "score_semantic": item.get("score_semantic"),
                    "score_final": item.get("score_final"),
                }
            )
        compact["items"] = compact_items[:30]
        compact["returned_count"] = len(compact["items"])
        compact["tool_result_count"] = len(compact_items)

        parsed = result.get("parsed")
        if isinstance(parsed, dict):
            compact["parsed"] = {
                key: parsed[key]
                for key in ("semantic", "from_date", "to_date", "place")
                if parsed.get(key) is not None
            }
        coverage = result.get("index_coverage")
        if isinstance(coverage, dict):
            compact["index_coverage"] = {
                key: coverage[key]
                for key in (
                    "complete",
                    "total_photos",
                    "indexed_photos",
                    "retrying_photos",
                    "unavailable_photos",
                    "coverage_ratio",
                    "faceted_photos",
                    "facet_coverage_ratio",
                    "semantic_complete",
                    "semantic_message",
                    "message",
                )
                if key in coverage
            }

    return json.dumps(compact, ensure_ascii=False, default=str)
