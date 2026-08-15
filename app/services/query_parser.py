"""自然语言查询解析：把"上个月西湖的雨天照片"拆成结构化条件。

真模式：调 qwen-plus，让它输出 JSON。
mock 模式：用一个小型规则引擎（正则匹配"今天/昨天/上周/上个月"等），
            这样即便没 DashScope Key 也能演示 auto_parse 的效果。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings
from app.schemas.photo import ParsedQuery
from app.services.circuit_breaker import ServiceDegradedError, agent_llm_breaker

logger = logging.getLogger(__name__)


_QWEN_TEXT_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
)


_SYSTEM_PROMPT = (
    "你是照片相册的查询解析器。用户会发一段中文搜索语句，"
    "请把它拆解成 JSON，字段包括："
    "semantic（去掉时间/地点后的语义部分，必填），"
    "from_date（起始日期 YYYY-MM-DD），"
    "to_date（结束日期 YYYY-MM-DD），"
    "place（地点名，如西湖/北京），"
    "tags（用户明确提到的相册标签或人物名，数组；普通物体、OCR文字不要当标签）。"
    "from_date/to_date 只表示照片的拍摄时间；台历、车票、登机牌、锁屏、"
    "菜单、标签、便签等画面内容中的日期必须保留在 semantic 中，日期字段设为 null。"
    "无法确定的字段设为 null。今天是 {today}。"
    "只输出 JSON，不要有任何解释文字。"
)


_CAPTURE_TIME_CUES = (
    "拍摄",
    "拍的",
    "拍于",
    "照的",
    "相册",
    "去年",
    "前年",
    "上周",
    "上个月",
    "昨天",
    "前天",
    "最近",
    "那天",
    "当天",
    "小时候",
)

_VISUAL_DATE_CUES = (
    "台历",
    "日历",
    "车票",
    "机票",
    "票据",
    "登机牌",
    "锁屏",
    "屏幕",
    "海报",
    "菜单",
    "标签",
    "便签",
    "写着",
    "显示",
)


def _is_mock() -> bool:
    return not settings.dashscope_api_key or settings.dashscope_api_key.strip() in (
        "",
        "sk-xxx",
        "please_set_dashscope_key",
    )


# ------------------------------------------------------------------
# mock 模式：规则版解析
# ------------------------------------------------------------------
def _rule_based_parse(text: str) -> ParsedQuery:
    """
    dev 用规则匹配几个常见词。真实产品当然要用大模型。
    这里覆盖：今天 / 昨天 / 前天 / 上周 / 上个月 / 今年 / X 月
    """
    today = datetime.now(timezone.utc).date()
    from_date: date | None = None
    to_date: date | None = None
    remaining = text

    patterns: list[tuple[str, tuple[date | None, date | None]]] = [
        ("今天", (today, today)),
        ("昨天", (today - timedelta(days=1), today - timedelta(days=1))),
        ("前天", (today - timedelta(days=2), today - timedelta(days=2))),
        ("最近一周", (today - timedelta(days=7), today)),
        ("这周", (today - timedelta(days=today.weekday()), today)),
        ("上周", (
            today - timedelta(days=today.weekday() + 7),
            today - timedelta(days=today.weekday() + 1),
        )),
        ("这个月", (today.replace(day=1), today)),
        ("上个月", _last_month_range(today)),
        ("今年", (today.replace(month=1, day=1), today)),
    ]
    for kw, rng in patterns:
        if kw in text:
            from_date, to_date = rng
            remaining = remaining.replace(kw, "")
            break

    # 简单挑几个常见地点
    place = None
    for name in ("西湖", "北京", "上海", "杭州", "成都", "海边", "沙滩", "公司", "家"):
        if name in remaining:
            place = name
            remaining = remaining.replace(name, "")
            break

    semantic = re.sub(r"\s+", " ", remaining).strip()
    if not semantic:
        semantic = text  # 兜底

    return ParsedQuery(
        semantic=semantic,
        from_date=from_date,
        to_date=to_date,
        place=place,
        tags=[],
    )


def _last_month_range(today: date) -> tuple[date, date]:
    first_this_month = today.replace(day=1)
    last_of_prev = first_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev, last_of_prev


# ------------------------------------------------------------------
# 真模式：调 qwen-plus
# ------------------------------------------------------------------
async def _llm_parse(text: str) -> ParsedQuery:
    """调 qwen-plus 解析查询。异常直接 raise，由 parse_query 统一降级。

    让异常传播使熔断器能正确追踪失败次数。
    """
    today = datetime.now(timezone.utc).date().isoformat()
    payload: dict[str, Any] = {
        "model": "qwen-plus",
        "input": {
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT.format(today=today)},
                {"role": "user", "content": text},
            ]
        },
        "parameters": {
            "result_format": "message",
            "temperature": 0.1,
            "max_tokens": 200,
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }

    # Keep DashScope traffic independent from stale desktop/system proxies.
    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
        resp = await client.post(_QWEN_TEXT_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"qwen-plus HTTP {resp.status_code}: {resp.text[:200]}")

    content = resp.json()["output"]["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    # 模型可能包一层 ```json ... ```
    content = content.strip().strip("`")
    if content.startswith("json"):
        content = content[4:].strip()

    data = json.loads(content)
    return ParsedQuery(
        semantic=data.get("semantic") or text,
        from_date=_maybe_date(data.get("from_date")),
        to_date=_maybe_date(data.get("to_date")),
        place=data.get("place"),
        tags=data.get("tags") or [],
    )


def _maybe_date(v) -> date | None:
    if not v or v == "null":
        return None
    try:
        return date.fromisoformat(v)
    except (ValueError, TypeError):
        return None


def should_apply_parsed_date_filters(text: str) -> bool:
    """仅在查询明确描述照片拍摄时间时接受 LLM 解析出的日期过滤。"""
    normalized = re.sub(r"\s+", "", text)
    if any(cue in normalized for cue in _VISUAL_DATE_CUES):
        return False
    return any(cue in normalized for cue in _CAPTURE_TIME_CUES)


def resolve_auto_parsed_query(
    text: str,
    parsed: ParsedQuery,
    *,
    from_date: date | None,
    to_date: date | None,
) -> tuple[str, date | None, date | None]:
    """把解析结果安全地合并到搜索参数。

    自动解析的普通 tags 不会在这里变成数据库硬过滤。日期只有在查询明确表达
    拍摄时间时才会补入；否则保留完整原句做向量查询，避免台历/OCR 日期丢失。
    """
    use_parsed_dates = should_apply_parsed_date_filters(text)
    resolved_from = from_date
    resolved_to = to_date
    if use_parsed_dates:
        if resolved_from is None:
            resolved_from = parsed.from_date
        if resolved_to is None:
            resolved_to = parsed.to_date

    has_accepted_parsed_date = use_parsed_dates and (
        parsed.from_date is not None or parsed.to_date is not None
    )
    if not has_accepted_parsed_date:
        return text.strip(), resolved_from, resolved_to

    parts = [parsed.semantic.strip()]
    if parsed.place and parsed.place not in parts[0]:
        parts.append(parsed.place)
    for tag in parsed.tags:
        clean_tag = str(tag).strip()
        if clean_tag and not any(clean_tag in part for part in parts):
            parts.append(clean_tag)
    effective_query = " ".join(part for part in parts if part).strip()
    return effective_query or text.strip(), resolved_from, resolved_to


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------
async def parse_query(text: str) -> ParsedQuery:
    """把自然语言查询拆成结构化条件。mock 模式走规则，真模式走 qwen-plus。

    真模式经 agent_llm_breaker 熔断器保护：
    - 熔断器 open 时直接降级到规则解析，不等待超时
    - LLM 调用失败时也降级到规则解析
    """
    if _is_mock():
        return _rule_based_parse(text)
    try:
        return await agent_llm_breaker.call(_llm_parse, text)
    except ServiceDegradedError:
        logger.warning("query_parser llm degraded, fallback to rule-based")
        return _rule_based_parse(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("query_parser llm failed, fallback to rule: %s", exc)
        return _rule_based_parse(text)
