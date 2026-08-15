"""Structured search-constraint extraction and candidate gating tests."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.photo import SearchQuery
from app.services.search_constraints import (
    StructuredConstraint,
    evaluate_candidate_constraints,
    extract_structured_constraints,
    validate_scored_candidates,
)


@pytest.mark.parametrize(
    ("query", "kind", "value"),
    [
        ("写着WORLD'S BEST BOSS的杯子", "visible_text", "WORLD'S BEST BOSS"),
        ("印着SONY标志的电视遥控器", "visible_text", "SONY"),
        ("《围城》的书籍封面", "visible_text", "围城"),
        ("价格是199元的衣服标签", "price", "199"),
        ("座位是3A的登机牌", "seat", "3A"),
        ("手机锁屏时间九点四十一", "time", "09:41"),
        ("2026年八月十五日的台历", "calendar_date", "2026-08-15"),
        ("北京南到上海虹桥的高铁票", "route", "北京南→上海虹桥"),
        ("冰箱上的买牛奶便签", "visible_text", "买牛奶"),
        ("依云矿泉水瓶", "entity", "依云"),
        ("全家便利店的招牌", "entity", "全家"),
        ("纽约时报的报纸", "entity", "纽约时报"),
    ],
)
def test_extracts_only_explicit_high_confidence_constraints(
    query: str, kind: str, value: str
) -> None:
    constraints = extract_structured_constraints(query)
    assert any(item.kind == kind and item.value == value for item in constraints)


def test_generic_semantic_query_has_no_constraint() -> None:
    assert extract_structured_constraints("两只狗在草地上奔跑") == []


def test_visible_text_normalizes_case_spaces_and_punctuation() -> None:
    constraint = StructuredConstraint("visible_text", "WORLD'S BEST BOSS", "test")
    matching = {
        "objects": ["咖啡杯"],
        "text_in_image": ["WORLD’S BEST BOSS"],
        "summary": "办公室桌面",
    }
    mismatch = {"objects": ["咖啡杯"], "text_in_image": ["WORLD'S BEST MOM"]}
    assert evaluate_candidate_constraints([constraint], matching).matches
    assert not evaluate_candidate_constraints([constraint], mismatch).matches


def test_numeric_slots_require_exact_token_not_substring() -> None:
    analysis = {"objects": ["价格标签"], "text_in_image": ["¥199", "2000199"]}
    assert evaluate_candidate_constraints(
        [StructuredConstraint("price", "199", "test")], analysis
    ).matches
    assert not evaluate_candidate_constraints(
        [StructuredConstraint("price", "99", "test")], analysis
    ).matches


def test_time_and_calendar_date_use_structured_evidence() -> None:
    phone = {"objects": ["智能手机"], "text_in_image": ["Monday, June 6", "9:41"]}
    calendar = {
        "objects": ["台历"],
        "text_in_image": ["八月 2026", "15", "星期六"],
    }
    assert evaluate_candidate_constraints(
        [StructuredConstraint("time", "09:41", "test")], phone
    ).matches
    assert not evaluate_candidate_constraints(
        [StructuredConstraint("time", "08:30", "test")], phone
    ).matches
    assert evaluate_candidate_constraints(
        [StructuredConstraint("calendar_date", "2026-08-15", "test")], calendar
    ).matches
    assert not evaluate_candidate_constraints(
        [StructuredConstraint("calendar_date", "2026-08-16", "test")], calendar
    ).matches


def test_route_direction_is_not_treated_as_an_unordered_bag_of_places() -> None:
    analysis = {
        "objects": ["火车票"],
        "text_in_image": ["北京南", "上海虹桥"],
        "summary": "一张从北京南站至上海虹桥站的高铁票",
    }
    assert evaluate_candidate_constraints(
        [StructuredConstraint("route", "北京南→上海虹桥", "test")], analysis
    ).matches
    assert not evaluate_candidate_constraints(
        [StructuredConstraint("route", "上海虹桥→北京南", "test")], analysis
    ).matches


def test_validate_scored_candidates_keeps_only_evidence_matches() -> None:
    matching = SimpleNamespace(
        ai_analysis={"objects": ["门垫"], "text_in_image": ["WELCOME"]},
        ai_description="写有 WELCOME 的门垫",
    )
    mismatch = SimpleNamespace(
        ai_analysis={"objects": ["门垫"], "text_in_image": ["HELLO"]},
        ai_description="写有 HELLO 的门垫",
    )
    constraints = extract_structured_constraints("写着WELCOME的门垫")

    kept, summary = validate_scored_candidates(
        [(matching, 1.0), (mismatch, 0.9)], constraints
    )

    assert kept == [(matching, 1.0)]
    assert summary is not None
    assert summary["matched_count"] == 1
    assert summary["rejected_count"] == 1


def test_visible_text_must_be_on_the_requested_object() -> None:
    constraints = extract_structured_constraints("写着HELLO的门垫")
    code_screen = {
        "objects": ["笔记本电脑", "代码编辑器"],
        "text_in_image": ["print('Hello World')", "Hello World"],
    }
    door_mat = {"objects": ["门垫"], "text_in_image": ["HELLO"]}

    assert not evaluate_candidate_constraints(constraints, code_screen).matches
    assert evaluate_candidate_constraints(constraints, door_mat).matches


def test_object_alias_matches_specific_vl_noun() -> None:
    constraints = extract_structured_constraints("写着WORLD'S BEST BOSS的杯子")
    coffee_mug = {
        "objects": ["咖啡杯"],
        "text_in_image": ["WORLD'S BEST BOSS"],
    }

    assert evaluate_candidate_constraints(constraints, coffee_mug).matches


@pytest.mark.asyncio
async def test_http_search_filters_explicit_ocr_conflict(monkeypatch) -> None:
    import app.api.search as search_api

    def photo(text: str):
        return SimpleNamespace(
            id=uuid4(),
            thumb_key="thumb.jpg",
            oss_key="photo.jpg",
            taken_at=None,
            ai_description=f"写有 {text} 的门垫",
            ai_analysis={"objects": ["门垫"], "text_in_image": [text]},
            status="done",
        )

    welcome = photo("WELCOME")
    hello = photo("HELLO")

    async def fake_embedding(_text: str) -> tuple[list[float], bool]:
        return [0.0] * 1024, False

    async def fake_profile(*_args):
        return None

    class Result:
        def all(self):
            return [(hello, 0.1), (welcome, 0.2)]

    class Db:
        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(search_api, "get_query_embedding", fake_embedding)
    monkeypatch.setattr(search_api, "get_user_profile", fake_profile)
    monkeypatch.setattr(search_api, "sign_get_url", lambda key: f"https://x/{key}")

    result = await search_api.semantic_search(
        SearchQuery(q="写着WELCOME的门垫", auto_parse=False, limit=5),
        SimpleNamespace(id=uuid4()),
        Db(),
    )

    assert [item.id for item in result.items] == [welcome.id]
    assert result.constraint_check is not None
    assert result.constraint_check.matched_count == 1
    assert result.constraint_check.rejected_count == 1


@pytest.mark.asyncio
async def test_agent_fallback_does_not_bypass_failed_strong_constraint(
    monkeypatch,
) -> None:
    import app.services.agent_tools as agent_tools

    async def no_match(**_kwargs):
        return {
            "ok": True,
            "items": [],
            "constraint_check": {"applied": True, "matched_count": 0},
            "hint": "no match",
        }

    async def forbidden_browse(**_kwargs):
        raise AssertionError("broad fallback must not bypass a strong constraint")

    monkeypatch.setattr(agent_tools, "search_photos", no_match)
    monkeypatch.setattr(agent_tools, "browse_candidates", forbidden_browse)

    result = await agent_tools.fallback_search(
        user_id=uuid4(),
        db=SimpleNamespace(),
        query="写着HELLO的门垫",
        start_level=1,
    )

    assert result["ok"] is True
    assert result["items"] == []
    assert result["constraint_check"]["applied"] is True
