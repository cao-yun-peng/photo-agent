"""Offline regressions from the sealed retrieval review plus counterexamples.

These exercise the rule layer only. Passing means a candidate reaches semantic
verification, not that it is a confirmed match or that end-to-end recall improved.
"""

import pytest

from app.services.search_constraints import (
    evaluate_candidate_constraints,
    extract_structured_constraints,
)


def check(query, *, text=(), objects=(), colors=(), description=""):
    return evaluate_candidate_constraints(
        extract_structured_constraints(query),
        {"text_in_image": list(text), "objects": list(objects), "colors": list(colors)},
        description,
    )


def test_descriptive_newspaper_request_is_not_a_proper_name():
    query = "找摊在桌上的人民日报报纸"
    assert not any(c.kind == "entity" for c in extract_structured_constraints(query))
    assert check(query, text=["人民日报"], objects=["报纸", "桌子"]).matches


def test_simple_explicit_entity_still_gates_evidence():
    query = "找光明日报报纸"
    assert [c.value for c in extract_structured_constraints(query)] == ["光明日报"]
    assert check(query, text=["光明日报"], objects=["报纸"]).matches
    assert not check(query, text=["人民日报"], objects=["报纸"]).matches


@pytest.mark.parametrize(
    "query", ["找不要人民日报的报纸", "找桌上摊开的报纸", "找有咖啡旁边的报纸"]
)
def test_relation_and_negative_clauses_are_deferred_to_semantic_verification(query):
    assert not any(c.kind == "entity" for c in extract_structured_constraints(query))


def test_color_and_carrier_evidence_need_not_be_adjacent():
    query = "找门口写着WELCOME的棕色门垫"
    assert check(query, text=["WELCOME"], objects=["门垫"], colors=["棕色"]).matches
    assert not check(query, text=["WELCOME"], objects=["杯子"], colors=["棕色"]).matches
    assert not check(query, text=["GOODBYE"], objects=["门垫"], colors=["棕色"]).matches


def test_symbol_description_is_not_concatenated_with_literal_ocr():
    query = "找白T恤上印着I、红色爱心和NY图案的照片"
    assert [(c.kind, c.value) for c in extract_structured_constraints(query)] == [
        ("visible_text", "I"),
        ("visible_text", "NY"),
    ]
    assert check(query, text=["I", "❤️", "NY"], objects=["T恤"]).matches
    assert check(query, text=["I ❤️ NY"], objects=["T恤"]).matches
    assert not check(query, text=["I", "❤️", "LA"], objects=["T恤"]).matches
    assert not check(query, text=["FIRST", "NY"], objects=["T恤"]).matches


@pytest.mark.parametrize("carrier", ["照片", "图片", "相片", "照片，必须包含环字"])
def test_generic_photo_is_not_a_required_object(carrier):
    query = f"找路牌清楚写着西四环南大街的{carrier}"
    assert not any(c.kind == "object" for c in extract_structured_constraints(query))
    assert check(query, text=["西四环南大街"], objects=["路牌"]).matches
    assert not check(query, text=["西四南大街"], objects=["路牌"]).matches


def test_mixed_ocr_list_preserves_each_required_phrase():
    query = "找有蓝底P、写着Mon-Sat和2 hours的白色停车牌"
    assert [(c.kind, c.value) for c in extract_structured_constraints(query)] == [
        ("visible_text", "Mon-Sat"),
        ("visible_text", "2 hours"),
        ("object", "停车牌"),
    ]
    assert check(
        query, text=["Mon - Sat", "8 am - 6 pm", "2 hours"], objects=["停车标志牌"]
    ).matches
    assert not check(
        query, text=["Mon - Sat", "12 hours"], objects=["停车标志牌"]
    ).matches
    assert not check(
        query, text=["Mon - Fri", "2 hours"], objects=["停车标志牌"]
    ).matches
    assert not check(query, text=["Mon - Sat", "2 hours"], objects=["菜单"]).matches


def test_parking_carrier_alias_preserves_time_text():
    query = "找写着8 am - 6 pm的停车牌"
    assert check(query, text=["8 am - 6 pm"], objects=["停车标志牌"]).matches
    assert not check(query, text=["9 am - 6 pm"], objects=["停车标志牌"]).matches


def test_speed_sign_with_intervening_number_and_negative_controls():
    query = "找白色牌面红色圆圈里写着50的限速标志"
    assert check(
        query, text=["50"], objects=["交通标志"], description="一个限速50的交通标志牌"
    ).matches
    assert not check(query, text=["30"], objects=["限速标志"]).matches
    assert not check(query, text=["150"], objects=["限速标志"]).matches
    assert not check(query, text=["50"], objects=["停车标志牌"]).matches


def test_storefront_alias_requires_storefront_context():
    query = "找写着McDonald's的门店招牌"
    assert check(
        query,
        text=["McDonald's"],
        objects=["麦当劳标志", "餐厅建筑", "广告牌"],
        description="从车内视角拍摄的麦当劳餐厅外观，配有标志和促销广告。",
    ).matches
    assert not check(
        query, text=["McDonald's"], objects=["广告牌"], description="杂志里的广告牌照片"
    ).matches
    assert not check(query, text=["Burger King"], objects=["门店招牌"]).matches


def test_storefront_context_is_not_a_brand_allowlist():
    query = "找写着Blue Bottle的门店招牌"
    assert check(
        query,
        text=["Blue Bottle"],
        objects=["广告牌"],
        description="咖啡店门口有一块广告牌。",
    ).matches


@pytest.mark.parametrize(
    ("query", "matching", "conflicting"),
    [
        ("找座位号12F的登机牌照片", "SEAT 12F", "SEAT 3A FIRST"),
        ("找售价50元的商品", "售价50元", "售价150元"),
        ("找写着50的交通标志", "50", "150"),
        ("找写着12F的登机牌", "12F", "112F"),
    ],
)
def test_explicit_identifiers_and_numbers_stay_strict(query, matching, conflicting):
    assert check(query, text=[matching], objects=["交通标志", "登机牌"]).matches
    assert not check(query, text=[conflicting], objects=["交通标志", "登机牌"]).matches


def test_text_does_not_cross_unrelated_evidence_fields():
    query = "找写着ABCD的杯子"
    assert not check(query, text=["AB", "CD"], objects=["杯子"]).matches


def test_quoted_conjunction_remains_literal():
    query = "找写着“Tea和Coffee”的杯子"
    assert [
        c.value
        for c in extract_structured_constraints(query)
        if c.kind == "visible_text"
    ] == ["Tea和Coffee"]
    assert check(query, text=["Tea和Coffee"], objects=["杯子"]).matches
    assert not check(query, text=["Tea", "Coffee"], objects=["杯子"]).matches


def test_separately_quoted_list_is_not_one_literal_phrase():
    query = "找写着“Mon-Sat”和“2 hours”的停车牌"
    assert check(query, text=["Mon - Sat", "2 hours"], objects=["停车标志牌"]).matches
    assert not check(
        query, text=["Mon - Sat", "12 hours"], objects=["停车标志牌"]
    ).matches


def test_chinese_name_is_not_split_on_conjunction_character():
    query = "找写着和平饭店的招牌"
    assert [
        c.value
        for c in extract_structured_constraints(query)
        if c.kind == "visible_text"
    ] == ["和平饭店"]


def test_disjunctive_text_does_not_become_an_and_constraint():
    query = "找写着OPEN或CLOSED的牌子"
    assert not any(
        c.kind == "visible_text" for c in extract_structured_constraints(query)
    )
