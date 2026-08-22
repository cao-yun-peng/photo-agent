"""Deterministic query policy and follow-up classification."""

from __future__ import annotations

import re

from app.services.agent_state import AgentState


_MORE_FOLLOWUP_PHRASES = (
    "还有一张",
    "再来一张",
    "再找一张",
    "换一张",
    "下一张",
    "还有吗",
    "还有没有",
    "更多",
    "别的呢",
    "其他的呢",
)
_USER_SELECTION_MARKERS = (
    "我自己选",
    "我来选",
    "让我选",
    "由我选",
    "自己选择",
    "全部给我",
    "都给我",
    "全给我",
    "拿到全部",
)
_PHOTO_COUNT_PATTERN = re.compile(r"(?P<count>\d{1,6})\s*张")
_COMPLETE_RESULT_MARKERS = ("全部", "所有", "全都", "一张不漏", "完整结果")
_PHOTO_RESULT_MARKERS = ("照片", "图片", "相片", "自拍", "合照", "截图", "候选")


def _requested_user_selection_limit(query: str) -> int | None:
    """识别“给我 N 张、由我自己选”，用于代码级兜底模型参数。"""

    normalized = "".join(str(query).split())
    if not any(marker in normalized for marker in _USER_SELECTION_MARKERS):
        return None
    match = _PHOTO_COUNT_PATTERN.search(normalized)
    if match is None:
        return None
    return max(1, int(match.group("count")))


def _requests_complete_result_set(query: str) -> bool:
    """识别“全部自拍/所有照片”等不允许按固定 N 截断的请求。"""

    normalized = "".join(str(query).split())
    return any(marker in normalized for marker in _COMPLETE_RESULT_MARKERS) and any(
        marker in normalized for marker in _PHOTO_RESULT_MARKERS
    )


def _detect_followup_type(query: str, state: AgentState) -> str | None:
    """识别依赖上一轮搜索目标的短追问；没有活动搜索时不猜测。"""
    if not state.active_search.get("resolved_query"):
        return None
    normalized = "".join(query.split()).strip("，。！？!?、")
    if len(normalized) <= 24 and any(
        phrase in normalized for phrase in _MORE_FOLLOWUP_PHRASES
    ):
        return "more_search_results"
    return None
