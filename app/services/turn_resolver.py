"""把一轮用户输入解析为可执行计划。

规则负责高置信度普通搜索、续搜和高风险/复杂请求分流；只有依赖上一轮
上下文的短句才调用一次文本模型。解析不确定时返回 Agent 兜底，绝不猜测
编辑、选择或删除等操作。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import httpx

from app.config import settings
from app.core.logger import get_logger
from app.schemas.photo import ParsedQuery
from app.services.circuit_breaker import agent_llm_breaker
from app.services.query_parser import (
    parse_query_locally,
    resolve_auto_parsed_query,
)
from app.services.search import (
    complete_scope_is_reliable,
    infer_complete_result_filters,
)
from app.utils.json_parser import parse_as_dict

logger = get_logger(__name__)

TurnIntent = Literal[
    "photo_search",
    "search_more",
    "result_feedback",
    "complex_agent",
    "unknown",
]
TurnRelation = Literal["new", "replace", "refine", "continue", "none"]

_MORE_PHRASES = (
    "还有一张",
    "再来一张",
    "再找一张",
    "换一张",
    "下一张",
    "还有吗",
    "还有没有",
    "有没有别的",
    "更多",
    "别的呢",
    "其他的呢",
)
_SEARCH_ACTIONS = ("找", "搜索", "搜一下", "查找", "给我看", "看看", "想要")
_PHOTO_WORDS = ("照片", "图片", "相片", "自拍", "合照", "截图")
_COMPLEX_MARKERS = (
    "最好",
    "最佳",
    "帮我选",
    "替我选",
    "选择",
    "全部",
    "所有",
    "拿到全部",
    "我自己选",
    "我来选",
    "让我选",
    "全部给我",
    "都给我",
    "全给我",
    "编辑",
    "修改",
    "生成",
    "修图",
    "滤镜",
    "风格化",
    "删除",
    "上传",
    "导出",
    "分享",
    "选择第",
    "我选",
    "选这张",
)
_QUESTION_MARKERS = (
    "怎么",
    "如何",
    "为什么",
    "能不能",
    "可以吗",
    "是什么",
    "什么意思",
    "谁",
    "哪张",
    "你会",
    "你能",
)
_VAGUE_SEARCHES = {
    "照片",
    "图片",
    "找照片",
    "找图片",
    "搜索照片",
    "搜索图片",
    "搜照片",
    "搜图片",
    "看看照片",
}
_QUICK_SEARCH_TERMS = {
    "最近一周",
    "上个月",
    "今年",
    "去年",
    "美食",
    "风景",
    "人像",
    "自拍",
    "合照",
}
_TRIM_PREFIX = re.compile(
    r"^(?:请|麻烦)?(?:帮我|给我|我想要|我想找|我想看)?"
    r"(?:找(?:\d+|一|几)?张|找一下|找找|查找|搜索一下|搜索|搜一下|搜搜|看看|看一下)?"
)
_NEGATIVE_RESULT_MARKERS = (
    "不要",
    "不需要",
    "不想要",
    "不对",
    "错了",
    "不符合",
    "不相关",
    "多余",
    "去掉",
    "排除",
)
_RESULT_REFERENCES = (
    "这张",
    "那张",
    "上一张",
    "刚才这张",
    "刚才那张",
    "这些照片",
    "这些图片",
    "结果里",
    "结果中",
    "你给的",
    "你给我",
    "有些",
    "有一张",
)
_POSITION_RE = re.compile(r"第(?P<position>\d+|[一二三四五六七八九十]+)张")
_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(slots=True)
class SearchTurn:
    """一次搜索所需的最小结构化参数。"""

    query: str
    from_date: date | None = None
    to_date: date | None = None
    place: str | None = None
    result_mode: Literal["browse", "select"] = "browse"
    limit: int = 5
    complete_result_set: bool = False
    retrieval_strategy: Literal[
        "vector_fast", "structured_complete", "exhaustive_semantic"
    ] = "vector_fast"

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "from_date": self.from_date.isoformat() if self.from_date else None,
            "to_date": self.to_date.isoformat() if self.to_date else None,
            "place": self.place,
            "result_mode": self.result_mode,
            "limit": self.limit,
            "complete_result_set": self.complete_result_set,
            "retrieval_strategy": self.retrieval_strategy,
        }


@dataclass(slots=True)
class ResultFeedback:
    """对当前结果集的显式负反馈。"""

    photo_ids: list[str] = field(default_factory=list)
    continue_search: bool = False
    search_query: str | None = None


@dataclass(slots=True)
class TurnPlan:
    """Turn Resolver 的稳定输出契约。"""

    intent: TurnIntent
    relation: TurnRelation
    confidence: float
    source: str
    search: SearchTurn | None = None
    feedback: ResultFeedback | None = None
    needs_clarification: bool = False
    clarification_question: str = ""
    clarification_options: list[str] = field(default_factory=list)
    model_calls: int = 0
    model_tokens: int = 0

    @property
    def can_use_search_fast_path(self) -> bool:
        return (
            self.intent == "photo_search"
            and self.search is not None
            and self.confidence >= 0.75
            and not self.needs_clarification
            and self.search.retrieval_strategy in {"vector_fast", "structured_complete"}
        )

    def route_payload(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "relation": self.relation,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "model_calls": self.model_calls,
        }


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip("，。！？!?、")


def _has_active_search(active_search: dict[str, Any] | None) -> bool:
    return bool((active_search or {}).get("resolved_query"))


def _position_value(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value in _CN_DIGITS:
        return _CN_DIGITS[value]
    if value.startswith("十"):
        return 10 + _CN_DIGITS.get(value[1:], 0)
    if value.endswith("十"):
        return _CN_DIGITS.get(value[:-1], 0) * 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        return _CN_DIGITS.get(tens, 0) * 10 + _CN_DIGITS.get(ones, 0)
    return None


def _feedback_clarification(items: list[dict[str, Any]]) -> TurnPlan:
    options = [
        f"第 {position} 张不需要"
        for position, item in enumerate(items[:5], start=1)
        if isinstance(item, dict) and item.get("id")
    ]
    return TurnPlan(
        "result_feedback",
        "refine",
        0.99,
        "rule",
        needs_clarification=True,
        clarification_question="你指的是哪一张？请告诉我序号，我会把它从当前结果和后续续搜中排除。",
        clarification_options=options,
    )


def _feedback_followup_query(query: str) -> str | None:
    text = _normalized(query)
    text = re.sub(r"^(?:不要|不需要|不想要)(?:刚才)?(?:这|那|上一)张", "", text)
    for phrase in _MORE_PHRASES:
        text = text.replace(phrase, "")
    text = text.strip("，。！？!?、 ")
    return _clean_search_query(text) if text else None


def _result_feedback_plan(
    query: str,
    *,
    active_search: dict[str, Any] | None,
    last_search_items: list[dict[str, Any]] | None,
    confirmed_photo_id: str | None,
) -> TurnPlan | None:
    """只解析有明确结果指代的负反馈，避免把“不要猫了”误判为拒图。"""

    text = _normalized(query)
    items = [
        item
        for item in (last_search_items or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if not (items or confirmed_photo_id) or not any(
        marker in text for marker in _NEGATIVE_RESULT_MARKERS
    ):
        return None

    position_match = _POSITION_RE.search(text)
    has_reference = bool(position_match) or "最后一张" in text or any(
        marker in text for marker in _RESULT_REFERENCES
    )
    if not has_reference:
        return None

    continue_search = any(phrase in text for phrase in _MORE_PHRASES)
    target_id: str | None = None
    if position_match:
        position = _position_value(position_match.group("position"))
        if position and position <= len(items):
            target_id = str(items[position - 1]["id"])
        else:
            return _feedback_clarification(items)
    elif "最后一张" in text or "上一张" in text:
        if items:
            target_id = str(items[-1]["id"])
    elif confirmed_photo_id and any(
        marker in text for marker in ("这张", "那张", "刚才这张", "刚才那张")
    ):
        target_id = str(confirmed_photo_id)
    elif len(items) == 1:
        target_id = str(items[0]["id"])
    else:
        return _feedback_clarification(items)

    if not target_id:
        return _feedback_clarification(items)
    return TurnPlan(
        "result_feedback",
        "continue" if continue_search else "refine",
        1.0,
        "rule",
        feedback=ResultFeedback(
            photo_ids=[target_id],
            continue_search=continue_search,
            search_query=(
                None
                if _has_active_search(active_search)
                else _feedback_followup_query(query)
            ),
        ),
    )


def _is_complex_or_non_search(text: str) -> bool:
    if any(marker in text for marker in _COMPLEX_MARKERS):
        return True
    return any(marker in text for marker in _QUESTION_MARKERS)


def _selection_search_turn(query: str) -> SearchTurn | None:
    """识别数量明确或要求全部交付、并由用户选择的确定性搜索。"""

    text = _normalized(query)
    has_photo_target = any(marker in text for marker in _PHOTO_WORDS) or any(
        marker in text for marker in ("我拍的", "拍过的", "相册里的", "相册中")
    )
    count_match = re.search(r"(?P<count>\d{1,6})张", text)
    wants_all = any(marker in text for marker in ("全部", "所有", "全都", "一张不漏"))
    user_selects = any(
        marker in text
        for marker in ("我自己选", "我来选", "让我选", "由我选", "自己选择", "从中选择")
    )
    if not has_photo_target or not (wants_all or (count_match and user_selects)):
        return None

    semantic = text
    for marker in (
        "把",
        "全部",
        "所有",
        "全都",
        "一张不漏",
        "都给我",
        "全给我",
        "给我",
        "我自己选",
        "我来选",
        "让我选",
        "由我选",
        "自己选择",
        "从中选择",
        "最好的一张",
    ):
        semantic = semantic.replace(marker, "")
    semantic = re.sub(r"\d{1,6}张", "", semantic).strip("，。！？!?、 ")
    semantic = semantic or "照片"
    parsed_search = _search_from_parsed(semantic)
    parsed_search.result_mode = "select"
    parsed_search.limit = int(count_match.group("count")) if count_match else 1
    parsed_search.complete_result_set = wants_all
    if wants_all:
        inferred = infer_complete_result_filters(semantic)
        parsed_search.retrieval_strategy = (
            "structured_complete"
            if complete_scope_is_reliable(inferred)
            else "exhaustive_semantic"
        )
    return parsed_search


def _looks_like_explicit_search(text: str) -> bool:
    if text in _QUICK_SEARCH_TERMS:
        return True
    has_action = any(marker in text for marker in _SEARCH_ACTIONS)
    has_photo_word = any(marker in text for marker in _PHOTO_WORDS)
    return has_photo_word or has_action


def _clean_search_query(text: str) -> str:
    cleaned = _TRIM_PREFIX.sub("", text.strip()).strip("，。！？!?、 ")
    cleaned = cleaned.lstrip("的")
    cleaned = re.sub(r"(?:给我)?(?:看一下|看看)$", "", cleaned).strip()
    return cleaned or text.strip()


def _search_from_parsed(text: str, parsed: ParsedQuery | None = None) -> SearchTurn:
    parsed = parsed or parse_query_locally(text)
    effective, from_date, to_date = resolve_auto_parsed_query(
        text,
        parsed,
        from_date=None,
        to_date=None,
    )
    cleaned = _clean_search_query(effective)
    if parsed.place and parsed.place not in cleaned:
        cleaned = f"{cleaned} {parsed.place}".strip()
    return SearchTurn(
        query=cleaned,
        from_date=from_date,
        to_date=to_date,
        place=parsed.place,
    )


def resolve_turn_by_rule(
    query: str,
    *,
    active_search: dict[str, Any] | None = None,
    last_search_items: list[dict[str, Any]] | None = None,
    confirmed_photo_id: str | None = None,
) -> TurnPlan | None:
    """返回高置信度规则结果；None 表示需要一次上下文解析。"""

    text = _normalized(query)
    active = _has_active_search(active_search)
    if not text:
        return TurnPlan("unknown", "none", 0.0, "rule")
    feedback_plan = _result_feedback_plan(
        query,
        active_search=active_search,
        last_search_items=last_search_items,
        confirmed_photo_id=confirmed_photo_id,
    )
    if feedback_plan is not None:
        return feedback_plan
    lost_photo_reference = (
        not last_search_items
        and not confirmed_photo_id
        and any(reference in text for reference in ("这张照片", "那张照片"))
        and any(action in text for action in ("处理", "改造", "编辑", "修改", "修图"))
    )
    if lost_photo_reference:
        return TurnPlan(
            "complex_agent",
            "none",
            1.0,
            "rule",
            needs_clarification=True,
            clarification_question="你想处理哪张照片？请重新描述照片，或先搜索并选择一张。",
            clarification_options=["先搜索照片", "重新描述照片"],
        )
    if active and len(text) <= 24 and any(phrase in text for phrase in _MORE_PHRASES):
        return TurnPlan("search_more", "continue", 1.0, "rule")
    if selection_search := _selection_search_turn(query):
        return TurnPlan(
            "photo_search",
            "replace" if active else "new",
            0.99,
            "rule",
            search=selection_search,
        )
    if _is_complex_or_non_search(text):
        return TurnPlan("complex_agent", "none", 0.98, "rule")
    if text in _VAGUE_SEARCHES:
        return TurnPlan(
            "photo_search",
            "replace" if active else "new",
            0.96,
            "rule",
            needs_clarification=True,
            clarification_question="你想找什么内容、人物、地点或时间范围的照片？",
            clarification_options=["最近一周", "自拍", "风景", "美食"],
        )
    if _looks_like_explicit_search(text):
        return TurnPlan(
            "photo_search",
            "replace" if active else "new",
            0.94,
            "rule",
            search=_search_from_parsed(query),
        )
    if not active:
        return TurnPlan("complex_agent", "none", 0.65, "rule")
    # 有活动搜索时，“金毛的呢 / 去年拍的 / 海边的”之类必须结合上下文。
    if len(text) <= 40:
        return None
    return TurnPlan("complex_agent", "none", 0.65, "rule")


def _is_mock_llm() -> bool:
    return not settings.dashscope_api_key or settings.dashscope_api_key.strip() in {
        "",
        "sk-xxx",
        "please_set_dashscope_key",
    }


async def _resolve_contextual_with_llm(
    query: str,
    *,
    active_search: dict[str, Any],
    recent_messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """用一次小型结构化调用合并意图识别和查询解析。"""

    system = (
        "你是照片助手的单轮路由器。根据当前搜索状态和用户新输入，只输出 JSON。"
        "intent 只能是 photo_search 或 complex_agent；relation 只能是 refine、replace、none。"
        "若用户在上一轮条件上增加或修改条件，输出 photo_search/refine，并让 query 是合并后的完整搜索句。"
        "若用户转向编辑、生成、选择、上传、删除、能力问答或闲聊，输出 complex_agent/none。"
        "搜索 JSON 字段：intent, relation, query, from_date, to_date, place, confidence。"
        "日期格式 YYYY-MM-DD，无法确定填 null。不要输出解释。"
    )
    context = {
        "active_search": {
            "resolved_query": active_search.get("resolved_query"),
            "filters": active_search.get("filters", {}),
        },
        "recent_messages": recent_messages[-4:],
        "user_input": query,
    }

    async def _call() -> tuple[dict[str, Any], int]:
        payload = {
            "model": settings.qwen_chat_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "temperature": 0.0,
            "max_tokens": 240,
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(8.0, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                settings.dashscope_chat_url,
                json=payload,
                headers=headers,
            )
        if response.status_code != 200:
            raise RuntimeError(f"turn resolver HTTP {response.status_code}")
        body = response.json()
        content = body["choices"][0]["message"].get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        usage = int((body.get("usage") or {}).get("total_tokens", 0) or 0)
        return parse_as_dict(content), usage

    return await agent_llm_breaker.call(_call)


def _date_or_none(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


async def resolve_turn(
    query: str,
    *,
    active_search: dict[str, Any] | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
    last_search_items: list[dict[str, Any]] | None = None,
    confirmed_photo_id: str | None = None,
) -> TurnPlan:
    """解析每轮输入；失败时安全回退到现有完整 Agent。"""

    rule_plan = resolve_turn_by_rule(
        query,
        active_search=active_search,
        last_search_items=last_search_items,
        confirmed_photo_id=confirmed_photo_id,
    )
    if rule_plan is not None:
        return rule_plan

    active_search = active_search or {}
    if _is_mock_llm():
        previous = str(active_search.get("resolved_query", "")).strip()
        combined = f"{previous} {_clean_search_query(query)}".strip()
        return TurnPlan(
            "photo_search",
            "refine",
            0.78,
            "rule_fallback",
            search=_search_from_parsed(combined),
        )

    try:
        raw, tokens = await _resolve_contextual_with_llm(
            query,
            active_search=active_search,
            recent_messages=recent_messages or [],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("turn resolver degraded; fallback to full agent: %s", exc)
        return TurnPlan("complex_agent", "none", 0.0, "fallback", model_calls=1)

    intent = str(raw.get("intent", ""))
    relation = str(raw.get("relation", ""))
    confidence_value = raw.get("confidence", 0.0)
    try:
        confidence = min(1.0, max(0.0, float(confidence_value)))
    except (TypeError, ValueError):
        confidence = 0.0
    if intent != "photo_search" or relation not in {"refine", "replace"}:
        return TurnPlan(
            "complex_agent",
            "none",
            confidence,
            "llm",
            model_calls=1,
            model_tokens=tokens,
        )
    resolved_query = str(raw.get("query", "")).strip()
    if not resolved_query:
        return TurnPlan(
            "complex_agent", "none", 0.0, "fallback", model_calls=1, model_tokens=tokens
        )
    search = SearchTurn(
        query=resolved_query,
        from_date=_date_or_none(raw.get("from_date")),
        to_date=_date_or_none(raw.get("to_date")),
        place=str(raw.get("place") or "").strip() or None,
    )
    return TurnPlan(
        "photo_search",
        relation,  # type: ignore[arg-type]
        confidence,
        "llm",
        search=search,
        model_calls=1,
        model_tokens=tokens,
    )
