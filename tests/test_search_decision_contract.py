"""Search-only output contract; real 2026-09-09 responses replayed offline.

The two response contents below are verbatim from sealed provider files
7a90ff0990904bf9bc7818f4b0fda0ad.json (val-008) and
22febb63ca4545b496aec0488e674676.json (val-013). They are copied here so
regressions run without the evaluation archive, credentials, Redis or models.
Acceptance of a response does not establish that its verdicts are correct.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import settings
from app.services import search_reranker as text_judge
from app.services import search_visual_verifier as visual_judge
from app.services.circuit_breaker import CircuitBreaker
from app.utils.json_parser import parse_as_dict

REAL_RESPONSES = {
    "val-008": "[\n  {\n    \"candidate_key\": \"c0\",\n    \"verdict\": \"match\",\n    \"confidence\": 0.98,\n    \"rationale\": \"证据明确显示室内书架上横着叠成一大摞的书籍，书脊颜色多样（紫色、黄色、蓝色、橙色、绿色、粉色），符合‘许多不同颜色的书横着叠成一大摞’的核心要求；场景为静态堆叠，无人员干扰，空间布局和色彩描述充分支持查询。\"\n  },\n  {\n    \"candidate_key\": \"c1\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 0.95,\n    \"rationale\": \"证据明确显示仅有一本被手持翻开的书，且人物正在阅读，不符合‘许多不同颜色的书横着叠成一大摞’的主体数量、堆叠状态及无动作干预的要求。\"\n  },\n  {\n    \"candidate_key\": \"c2\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 0.97,\n    \"rationale\": \"证据明确显示场景为户外红砖墙边的编织篮子，无任何书籍出现，与查询中‘书’这一关键主体完全冲突。\"\n  },\n  {\n    \"candidate_key\": \"c3\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 0.99,\n    \"rationale\": \"证据明确显示主体为甜甜圈展示柜，所有对象均为食品及包装，无任何书籍存在，与查询主体‘书’直接矛盾。\"\n  },\n  {\n    \"candidate_key\": \"c4\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 0.96,\n    \"rationale\": \"证据明确显示主体为涂鸦建筑与自行车，无书籍元素，与查询中‘书’这一必要主体完全不符。\"\n  }\n]",
    "val-013": "[\n  {\n    \"candidate_key\": \"c0\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 0.95,\n    \"rationale\": \"查询要求‘浅色奶油’，但候选明确描述为‘花生酱并撒有花生碎’，二者成分与颜色（花生酱为棕色，非浅色）均冲突；且‘部分被咬过的牛角面包’与查询未提咬痕但强调‘托着’的完整呈现状态存在动作/完整性矛盾。\"\n  },\n  {\n    \"candidate_key\": \"c1\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 1.0,\n    \"rationale\": \"场景为甜甜圈展示柜，无手、无可颂、无奶油、无红色座椅，主体与所有关键要素均缺失且场景完全不符。\"\n  },\n  {\n    \"candidate_key\": \"c2\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 1.0,\n    \"rationale\": \"主体为坐轮椅男子操作锅具，无可颂、无奶油、无手托食物动作、无红色座椅背景（仅有红色轮椅，非座椅），场景为厨房，与查询完全无关。\"\n  },\n  {\n    \"candidate_key\": \"c3\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 1.0,\n    \"rationale\": \"主体为俯拍咖啡杯，无可颂、无手、无红色座椅，场景与对象均与查询彻底冲突。\"\n  },\n  {\n    \"candidate_key\": \"c4\",\n    \"verdict\": \"contradiction\",\n    \"confidence\": 1.0,\n    \"rationale\": \"主体为街道上骑行者，无可颂、无奶油、无手托食物、无红色座椅，场景为户外街道，与查询完全不符。\"\n  }\n]"
}

PARSERS = [text_judge._parse_decisions, visual_judge._parse_decisions]
ROW = {
    "candidate_key": "c0",
    "verdict": "match",
    "confidence": 0.95,
    "rationale": "猫闭着眼趴卧。",
}


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("query_id", REAL_RESPONSES)
def test_real_array_responses_are_preserved(parse, query_id):
    content = REAL_RESPONSES[query_id]
    # Reproduce the old envelope failure, then recover all five actual rows.
    assert parse_as_dict(content) == {}
    rows = parse(content, {f"c{i}" for i in range(5)})
    assert rows is not None
    assert [item.as_dict() for item in rows] == json.loads(content)
    assert rows[0].verdict == ("match" if query_id == "val-008" else "contradiction")


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("wrapper", ["object", "array", "json_object", "json_array", "fence", "plain_fence"])
def test_complete_supported_envelopes(parse, wrapper):
    value = {"decisions": [ROW]} if "object" in wrapper else [ROW]
    if wrapper.startswith("json") or "fence" in wrapper:
        value = json.dumps(value, ensure_ascii=False)
    if wrapper == "fence":
        value = "```json\n" + value + "\n```"
    if wrapper == "plain_fence":
        value = "```\n" + value + "\n```"
    rows = parse(value, {"c0"})
    assert rows is not None and rows[0].as_dict() == ROW


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("confidence", [None, True, False, "0.9", "bad", -0.1, 1.1, float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_confidence_never_becomes_a_match(parse, confidence):
    assert parse([dict(ROW, confidence=confidence)], {"c0"}) is None


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("field", list(ROW))
def test_missing_fields_reject_batch(parse, field):
    incomplete = dict(ROW)
    incomplete.pop(field)
    assert parse([incomplete], {"c0"}) is None


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("rows,expected", [
    ([], {"c0"}),
    ([ROW], {"c0", "c1"}),
    ([ROW, ROW], {"c0"}),
    ([ROW, dict(ROW, candidate_key="unknown")], {"c0"}),
    ([ROW, {"bad": "item"}], {"c0"}),
    ([ROW, None], {"c0"}),
    ([dict(ROW, candidate_key=0)], {"0"}),
    ([dict(ROW, candidate_key="")], {""}),
    ([dict(ROW, verdict="yes")], {"c0"}),
    ([dict(ROW, verdict=None)], {"c0"}),
    ([dict(ROW, rationale={"text": "cat"})], {"c0"}),
])
def test_bad_rows_cannot_be_skipped_to_appear_complete(parse, rows, expected):
    assert parse(rows, expected) is None


@pytest.mark.parametrize("parse", PARSERS)
@pytest.mark.parametrize("document", [
    "prefix " + json.dumps([ROW]),
    json.dumps([ROW]) + " trailing explanation",
    json.dumps([ROW])[:-1],
    "```json\n" + json.dumps([ROW]),
    "```python\n" + json.dumps([ROW]) + "\n```",
    json.dumps([ROW]) + json.dumps([ROW]),
    '[{"candidate_key":"c0","verdict":"contradiction","verdict":"match","confidence":0.9,"rationale":"x"}]',
    '[{"candidate_key":"c0","verdict":"match","confidence":NaN,"rationale":"x"}]',
    '[{"candidate_key":"c0","verdict":"match","confidence":Infinity,"rationale":"x"}]',
    '[{"candidate_key":"c0","verdict":"match","confidence":1e999,"rationale":"x"}]',
    '{"decisions":[],"decisions":' + json.dumps([ROW]) + '}',
    "null",
])
def test_no_inner_fragment_or_malformed_json_recovery(parse, document):
    assert parse(document, {"c0"}) is None


@pytest.mark.parametrize("parse", PARSERS)
def test_existing_normalization_and_empty_batch_contract(parse):
    rows = parse({"decisions": [dict(ROW, verdict="MATCH", rationale="字" * 300)]}, {"c0"})
    assert rows[0].verdict == "match"
    assert rows[0].rationale == "字" * 240
    assert parse([], set()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "visual"])
@pytest.mark.parametrize("query_id", REAL_RESPONSES)
async def test_provider_path_uses_search_contract_and_keeps_usage(monkeypatch, kind, query_id):
    module = text_judge if kind == "text" else visual_judge
    usage = {"input_tokens": 11, "output_tokens": 7}
    raw_text = REAL_RESPONSES[query_id]
    payload = (
        {"choices": [{"message": {"content": raw_text}}], "usage": usage}
        if kind == "text"
        else {"output": {"choices": [{"message": {"content": [{"text": raw_text}]}}]}, "usage": usage}
    )
    requests = []

    class RecordedClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return httpx.Response(200, json=payload)

    monkeypatch.setattr(module.httpx, "AsyncClient", RecordedClient)
    monkeypatch.setattr(module, "_is_mock", lambda: False)
    record_usage = AsyncMock()
    monkeypatch.setattr(module, "record_provider_usage", record_usage)
    monkeypatch.setattr(settings, "search_visual_verify_enabled", True)
    if kind == "text":
        monkeypatch.setattr(module, "search_rerank_breaker", CircuitBreaker("offline-text"))
        candidates = [{"candidate_key": f"c{i}"} for i in range(5)]
        rows, meta = await module._judge_candidate_evidence("原始响应回放", candidates, use_cache=False)
    else:
        monkeypatch.setattr(module, "search_visual_verify_breaker", CircuitBreaker("offline-visual"))
        monkeypatch.setattr(module, "sign_get_url", lambda *args, **kwargs: "https://example.invalid/test.jpg")
        candidates = [module.VisualCandidate(f"c{i}", str(i), "test.jpg", "hash") for i in range(5)]
        rows, meta = await module._judge_visual_candidates("原始响应回放", candidates, use_cache=False)
    assert len(requests) == 1
    assert [row.as_dict() for row in rows] == json.loads(raw_text)
    record_usage.assert_awaited_once_with(usage)
    assert meta["cache_hit"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "visual"])
async def test_contract_version_partitions_existing_cache(monkeypatch, kind):
    from app.services import search_cache

    module = text_judge if kind == "text" else visual_judge
    keys = []

    def key_for(stage, value):
        keys.append(value)
        return "offline"

    async def cached_call(key, compute, *, ttl, validate):
        return validate(await compute()), False

    monkeypatch.setattr(search_cache, "cache_key", key_for)
    monkeypatch.setattr(search_cache, "cached_call", cached_call)
    monkeypatch.setattr(settings, "search_visual_verify_enabled", True)
    if kind == "text":
        provider = AsyncMock(return_value=([module.RerankDecision(**ROW)], {}))
        monkeypatch.setattr(module, "_judge_candidate_evidence", provider)
        candidates = [{"candidate_key": "c0"}]
        judge = module.judge_candidate_evidence
    else:
        provider = AsyncMock(return_value=([module.VisualDecision(**ROW)], {}))
        monkeypatch.setattr(module, "_judge_visual_candidates", provider)
        candidates = [module.VisualCandidate("c0", "id", "test.jpg", "hash")]
        judge = module.judge_visual_candidates
    await judge("cat", candidates)
    original_version = module.DECISION_CONTRACT_VERSION
    monkeypatch.setattr(module, "DECISION_CONTRACT_VERSION", "future-test-contract")
    await judge("cat", candidates)
    assert original_version in keys[0]
    assert "future-test-contract" in keys[1]
    assert keys[0] != keys[1]
