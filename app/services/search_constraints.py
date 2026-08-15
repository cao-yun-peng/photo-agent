"""Conservative structured-constraint validation for photo search candidates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Sequence, TypeVar

from app.models.photo import Photo


@dataclass(frozen=True)
class StructuredConstraint:
    """A high-confidence requirement explicitly stated by the user."""

    kind: str
    value: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "source": self.source}


@dataclass(frozen=True)
class ConstraintDecision:
    """Whether one candidate has evidence for every structured requirement."""

    matches: bool
    failed_kinds: tuple[str, ...]


_Scored = TypeVar("_Scored", bound=tuple[Any, ...])

_WRITTEN_TEXT_RE = re.compile(r"写着(?P<value>.+?)的")
_WRITTEN_TARGET_RE = re.compile(r"写着.+?的(?P<value>[^的]{1,16})$")
_PRINTED_TEXT_RE = re.compile(
    r"印着(?P<value>.+?)(?:标志|字样|图案)?的", re.IGNORECASE
)
_PRINTED_TARGET_RE = re.compile(r"印着.+?的(?P<value>[^的]{1,16})$", re.IGNORECASE)
_BOOK_TITLE_RE = re.compile(r"《(?P<value>[^》]{1,40})》")
_PRICE_RE = re.compile(
    r"(?:价格|售价)(?:是|为)?\s*[¥￥]?\s*(?P<value>\d+(?:\.\d+)?)\s*元?",
    re.IGNORECASE,
)
_SEAT_RE = re.compile(
    r"座位(?:号|是|为)?\s*(?P<value>[A-Z0-9-]{1,8})", re.IGNORECASE
)
_TIME_RE = re.compile(
    r"(?:锁屏)?时间(?:是|为)?\s*(?P<hour>[零〇一二两三四五六七八九十百\d]+)点"
    r"(?P<minute>[零〇一二两三四五六七八九十百\d]+)?分?"
)
_DATE_RE = re.compile(
    r"(?P<year>\d{4})年(?P<month>[零〇一二两三四五六七八九十\d]+)月"
    r"(?P<day>[零〇一二两三四五六七八九十\d]+)[日号]"
)
_ROUTE_RE = re.compile(
    r"(?P<origin>[\u4e00-\u9fffA-Za-z0-9]+?)(?:站)?(?:到|至)"
    r"(?P<destination>[\u4e00-\u9fffA-Za-z0-9]+?)(?:站)?(?:的)?"
    r"(?:高铁票|火车票|车票|机票|航班)"
)
_NOTE_RE = re.compile(r"冰箱上(?:的|写着)(?P<value>.+?)(?:的)?便签")
_ENTITY_PATTERNS = (
    re.compile(
        r"^(?P<value>[\u4e00-\u9fffA-Za-z0-9·' -]{2,20})"
        r"(?:可乐罐|矿泉水瓶|门店招牌|电视遥控器)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<value>[\u4e00-\u9fffA-Za-z0-9·' -]{2,16})"
        r"便利店(?:的)?招牌$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<value>[\u4e00-\u9fffA-Za-z0-9·' -]{2,20})(?:的)?报纸$",
        re.IGNORECASE,
    ),
)

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_OBJECT_ALIASES = {
    "盒子": ("盒子", "包装盒", "纸盒", "牛奶盒", "盒"),
    "杯子": ("杯子", "杯", "咖啡杯", "水杯"),
    "电视遥控器": ("电视遥控器", "遥控器"),
    "书籍封面": ("书籍封面", "书籍", "图书", "封面"),
    "交通标志": ("交通标志", "交通牌", "路牌", "标志"),
    "衣服标签": ("衣服标签", "价格标签", "衣物", "标签"),
}
_OBJECT_PREFIXES = ("蓝色", "红色", "白色", "黑色", "绿色", "黄色")


def _cn_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value in _CN_DIGITS:
        return _CN_DIGITS[value]
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _clean_value(value: str) -> str:
    return value.strip(" \t\r\n，。！？、:：;；'\"").removesuffix("的").strip()


def extract_structured_constraints(query: str) -> list[StructuredConstraint]:
    """Extract only constraints that are explicit enough for evidence gating."""

    found: list[StructuredConstraint] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, source: str) -> None:
        cleaned = _clean_value(value)
        key = (kind, _normalize(cleaned))
        if cleaned and key[1] and key not in seen:
            seen.add(key)
            found.append(StructuredConstraint(kind=kind, value=cleaned, source=source))

    for pattern, source in (
        (_WRITTEN_TEXT_RE, "写着…的"),
        (_PRINTED_TEXT_RE, "印着…的"),
        (_BOOK_TITLE_RE, "书名号"),
        (_NOTE_RE, "便签内容"),
    ):
        for match in pattern.finditer(query):
            add("visible_text", match.group("value"), source)

    for pattern, source in (
        (_WRITTEN_TARGET_RE, "文字承载物"),
        (_PRINTED_TARGET_RE, "文字承载物"),
    ):
        if match := pattern.search(query):
            add("object", match.group("value"), source)

    if _BOOK_TITLE_RE.search(query):
        add("object", "书籍封面", "书名承载物")

    if match := _PRICE_RE.search(query):
        add("price", match.group("value"), "价格")
    if match := _SEAT_RE.search(query):
        add("seat", match.group("value"), "座位号")
    if match := _TIME_RE.search(query):
        hour = _cn_number(match.group("hour"))
        minute = _cn_number(match.group("minute") or "零")
        if hour is not None and minute is not None and 0 <= hour <= 23 and 0 <= minute <= 59:
            add("time", f"{hour:02d}:{minute:02d}", "锁屏时间")
    if match := _DATE_RE.search(query):
        month = _cn_number(match.group("month"))
        day = _cn_number(match.group("day"))
        if month is not None and day is not None and 1 <= month <= 12 and 1 <= day <= 31:
            add(
                "calendar_date",
                f"{int(match.group('year')):04d}-{month:02d}-{day:02d}",
                "画面日期",
            )
    if match := _ROUTE_RE.search(query):
        origin = _clean_station(match.group("origin"))
        destination = _clean_station(match.group("destination"))
        if origin and destination:
            add("route", f"{origin}→{destination}", "起终点")

    if not any(item.kind == "visible_text" for item in found):
        stripped = query.strip()
        for pattern in _ENTITY_PATTERNS:
            if match := pattern.match(stripped):
                add("entity", match.group("value"), "品牌或专名")
                break

    return found


def _analysis_fragments(analysis: dict[str, Any], description: str | None) -> list[str]:
    fragments: list[str] = []
    for key in ("text_in_image", "objects", "colors"):
        value = analysis.get(key)
        if isinstance(value, list):
            fragments.extend(str(item) for item in value if item)
    for key in ("scene", "scene_detail", "mood", "summary"):
        value = analysis.get(key)
        if value:
            fragments.append(str(value))
    if description:
        fragments.append(description)
    return fragments


def _number_tokens(fragments: Iterable[str]) -> set[str]:
    return {
        token
        for fragment in fragments
        for token in re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])", fragment)
    }


def _seat_tokens(fragments: Iterable[str]) -> set[str]:
    return {
        token.casefold()
        for fragment in fragments
        for token in re.findall(r"(?<![A-Za-z0-9])(?:\d+[A-Za-z]|[A-Za-z]\d+)(?![A-Za-z0-9])", fragment)
    }


def _time_tokens(fragments: Iterable[str]) -> set[str]:
    times = set()
    for fragment in fragments:
        for hour, minute in re.findall(
            r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", fragment
        ):
            times.add(f"{int(hour):02d}:{int(minute):02d}")
    return times


def _clean_station(value: str) -> str:
    cleaned = _clean_value(value)
    for suffix in ("火车站", "高铁站", "车站", "站"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.removeprefix("从")


def _route_matches(value: str, fragments: Sequence[str]) -> bool:
    origin, destination = (_clean_station(part) for part in value.split("→", 1))
    combined = " ".join(fragments)
    route_pattern = re.compile(
        r"从(?P<origin>[\u4e00-\u9fffA-Za-z0-9]+?)(?:站)?(?:到|至|飞往)"
        r"(?P<destination>[\u4e00-\u9fffA-Za-z0-9]+?)(?:站|的|，|,|\s|$)"
    )
    if match := route_pattern.search(combined):
        candidate_origin = _clean_station(match.group("origin"))
        candidate_destination = _clean_station(match.group("destination"))
        return (
            _normalize(candidate_origin) == _normalize(origin)
            and _normalize(candidate_destination) == _normalize(destination)
        )

    normalized = _normalize(combined)
    origin_index = normalized.find(_normalize(origin))
    destination_index = normalized.find(_normalize(destination))
    return 0 <= origin_index < destination_index


def _calendar_date_matches(
    value: str, analysis: dict[str, Any], fragments: Sequence[str]
) -> bool:
    objects = " ".join(str(item) for item in analysis.get("objects", []))
    if "台历" not in objects and "日历" not in objects:
        return False
    year, month_text, day_text = value.split("-")
    month, day = int(month_text), int(day_text)
    combined = " | ".join(fragments)
    month_cn = "一二三四五六七八九十"[month - 1] if month <= 10 else None
    month_present = bool(
        re.search(rf"(?<!\d)0?{month}月", combined)
        or (month_cn and f"{month_cn}月" in combined)
        or (month == 11 and "十一月" in combined)
        or (month == 12 and "十二月" in combined)
    )
    day_present = bool(re.search(rf"(?<!\d)0?{day}(?!\d)", combined))
    return year in combined and month_present and day_present


def _object_matches(value: str, analysis: dict[str, Any], fragments: Sequence[str]) -> bool:
    cleaned = value
    for prefix in _OBJECT_PREFIXES:
        cleaned = cleaned.removeprefix(prefix)
    aliases = _OBJECT_ALIASES.get(cleaned, (cleaned,))
    corpus = _normalize(" ".join(fragments))
    return any(_normalize(alias) in corpus for alias in aliases)


def evaluate_candidate_constraints(
    constraints: Sequence[StructuredConstraint],
    analysis: dict[str, Any] | None,
    description: str | None = None,
) -> ConstraintDecision:
    """Require positive evidence for every extracted high-confidence constraint."""

    if not constraints:
        return ConstraintDecision(matches=True, failed_kinds=())
    analysis = analysis or {}
    fragments = _analysis_fragments(analysis, description)
    normalized_corpus = _normalize(" ".join(fragments))
    numbers = _number_tokens(fragments)
    seats = _seat_tokens(fragments)
    times = _time_tokens(fragments)
    failures = []

    for constraint in constraints:
        expected = constraint.value
        if constraint.kind in {"visible_text", "entity"}:
            matched = _normalize(expected) in normalized_corpus
        elif constraint.kind == "price":
            matched = expected in numbers
        elif constraint.kind == "seat":
            matched = expected.casefold() in seats
        elif constraint.kind == "time":
            matched = expected in times
        elif constraint.kind == "calendar_date":
            matched = _calendar_date_matches(expected, analysis, fragments)
        elif constraint.kind == "route":
            matched = _route_matches(expected, fragments)
        elif constraint.kind == "object":
            matched = _object_matches(expected, analysis, fragments)
        else:
            matched = True
        if not matched:
            failures.append(constraint.kind)

    return ConstraintDecision(matches=not failures, failed_kinds=tuple(failures))


def validate_scored_candidates(
    scored: Sequence[_Scored],
    constraints: Sequence[StructuredConstraint],
) -> tuple[list[_Scored], dict[str, Any] | None]:
    """Filter scored tuples whose first item is a Photo ORM object."""

    if not constraints:
        return list(scored), None
    kept: list[_Scored] = []
    rejected = 0
    failures: dict[str, int] = {}
    for item in scored:
        photo: Photo = item[0]
        decision = evaluate_candidate_constraints(
            constraints,
            photo.ai_analysis,
            photo.ai_description,
        )
        if decision.matches:
            kept.append(item)
        else:
            rejected += 1
            for kind in set(decision.failed_kinds):
                failures[kind] = failures.get(kind, 0) + 1
    return kept, {
        "applied": True,
        "constraints": [constraint.as_dict() for constraint in constraints],
        "candidates_checked": len(scored),
        "matched_count": len(kept),
        "rejected_count": rejected,
        "rejected_by_kind": dict(sorted(failures.items())),
    }
