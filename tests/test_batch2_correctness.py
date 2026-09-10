from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError
from app.schemas.skill import SkillCreate, SkillUpdate
from app.services import query_parser as parser
from app.services.search_time import date_bounds
from app.services.search_constraints import extract_structured_constraints
from app.services.agent_execution import _parse_arguments, _prepare_arguments


@pytest.mark.parametrize(
    "schema,params",
    [(SkillCreate, {"name": "x", "prompt_template": "x"}), (SkillUpdate, {})],
)
def test_model_contract(schema, params):
    for model in ["wanx2.1-imageedit", "gpt-image-2"]:
        assert schema(**params, model=model).model == model
    with pytest.raises(ValidationError):
        schema(**params, model="wanx-v1")


def test_apply_skill_recovery_and_validation(monkeypatch):
    import app.services.agent_execution as execution

    monkeypatch.setattr(execution, "parse_as_dict", lambda _: {})
    pid = str(uuid4())
    args = _parse_arguments(
        "apply_skill",
        'broken {"photo_id":"' + pid + '","extra_prompt":"暖色"} trailing',
    )
    assert args["extra_prompt"] == "暖色"
    agent = SimpleNamespace(db=object())
    state = SimpleNamespace(followup_type=None)
    prepared, error = _prepare_arguments(agent, uuid4(), "apply_skill", args, state)
    assert error is None and prepared["photo_id"] == UUID(pid)
    for invalid in [
        {"extra_prompt": "x"},
        {"photo_id": "bad"},
        {"photo_id": pid, "extra_prompt": 123},
        {"photo_id": pid, "prompt": "x"},
    ]:
        assert (
            _prepare_arguments(agent, uuid4(), "apply_skill", invalid, state)[1][
                "error_type"
            ]
            == "invalid_arguments"
        )


@pytest.mark.parametrize(
    "zone,expected",
    [("Asia/Shanghai", date(2026, 9, 5)), ("America/Los_Angeles", date(2026, 9, 4))],
)
@pytest.mark.asyncio
async def test_rule_and_llm_share_local_today(zone, expected, monkeypatch):
    def clock():
        return datetime(2026, 9, 4, 17, tzinfo=timezone.utc)

    assert (
        parser.parse_query_locally(
            "今天拍的猫", timezone_name=zone, clock=clock
        ).from_date
        == expected
    )
    seen = []

    async def llm(text, *, today):
        seen.append(today)
        return parser._rule_based_parse(text, today=today)

    monkeypatch.setattr(parser, "_is_mock", lambda: False)
    monkeypatch.setattr(parser, "_llm_parse", llm)
    result = await parser.parse_query("今天拍的猫", timezone_name=zone, clock=clock)
    assert result.from_date == expected and seen == [expected]


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_dst_half_open_day(day, hours):
    start, end = date_bounds(day, day, "America/New_York")
    assert (end - start).total_seconds() == hours * 3600
    assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc


def test_shanghai_date_boundary():
    start, end = date_bounds(date(2026, 9, 5), date(2026, 9, 5), "Asia/Shanghai")
    assert start == datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 5, 16, tzinfo=timezone.utc)


def test_capture_date_is_not_visual_constraint():
    capture = "2026年9月5日拍摄的日历照片"
    assert not any(
        x.kind == "calendar_date" for x in extract_structured_constraints(capture)
    )
    parsed = parser.parse_query_locally(capture)
    assert parsed.from_date == date(2026, 9, 5)
    visual = "日历上写着2026年9月5日的照片"
    assert any(
        x.kind == "calendar_date" for x in extract_structured_constraints(visual)
    )
    assert parser.parse_query_locally(visual).from_date is None
    assert not any(
        x.kind == "calendar_date"
        for x in extract_structured_constraints("2026年9月5日的照片")
    )


@pytest.mark.asyncio
async def test_timezone_middleware_is_request_local():
    import asyncio
    import httpx
    from fastapi import FastAPI
    from app.services.search_time import SearchTimezoneMiddleware, current_timezone

    app = FastAPI()
    app.add_middleware(SearchTimezoneMiddleware)

    @app.get("/")
    async def read_zone():
        await asyncio.sleep(0.01)
        return {"zone": current_timezone()}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(
            client.get("/", headers={"X-Timezone": "America/New_York"}),
            client.get("/", headers={"X-Timezone": "Asia/Shanghai"}),
        )
        assert first.json()["zone"] == "America/New_York"
        assert second.json()["zone"] == "Asia/Shanghai"
        assert (
            await client.get("/", headers={"X-Timezone": "invalid-zone"})
        ).status_code == 422
        assert (await client.get("/")).json()["zone"] == "Asia/Shanghai"


def test_update_cannot_set_null_model():
    with pytest.raises(ValidationError):
        SkillUpdate(model=None)


def test_multiple_dates_preserve_visual_source_only():
    query = "2026年9月5日拍的日历上写着2025年8月1日的照片"
    dates = [
        item
        for item in extract_structured_constraints(query)
        if item.kind == "calendar_date"
    ]
    assert [item.value for item in dates] == ["2025-08-01"]
    parsed = parser.parse_query_locally(query)
    assert parsed.date_kind == "capture_time_range"
    assert parsed.from_date == date(2026, 9, 5)
    assert parsed.date_source == query
