from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.search_reranker import (
    RerankDecision,
    apply_rerank_decisions,
    evidence_from_scored,
    merge_visual_decisions,
    rerank_scored_candidates,
    verify_scored_candidate_pool,
    visual_trigger_reason,
)
from app.services.search_visual_verifier import VisualDecision
from scripts.evaluate_search_reranker import replay, validate_rerank_labels


def _row(photo_id: str, score: float, analysis: dict | None = None) -> tuple:
    photo = SimpleNamespace(
        id=photo_id,
        ai_analysis=analysis or {},
        ai_description=f"description-{photo_id}",
        oss_key=f"users/test/{photo_id}.jpg",
        hash=f"hash-{photo_id}",
    )
    return (photo, score, 0.0, 0.0, score)


def test_apply_rerank_rejects_only_high_confidence_contradictions() -> None:
    scored = [_row("p-1", 0.9), _row("p-2", 0.8), _row("p-3", 0.7)]
    decisions = [
        RerankDecision("c0", "uncertain", 0.9, "insufficient"),
        RerankDecision("c1", "match", 0.8, "supported"),
        RerankDecision("c2", "contradiction", 0.95, "wrong object"),
    ]

    reranked, counts = apply_rerank_decisions(
        scored, decisions, top_k=3, reject_confidence=0.8
    )

    assert [row[0].id for row in reranked] == ["p-2", "p-1"]
    assert counts == {
        "match": 1,
        "uncertain": 1,
        "contradiction": 1,
        "rejected": 1,
    }


def test_low_confidence_contradiction_is_kept_as_uncertain() -> None:
    scored = [_row("p-1", 0.9)]
    reranked, counts = apply_rerank_decisions(
        scored,
        [RerankDecision("c0", "contradiction", 0.79, "weak conflict")],
        top_k=1,
        reject_confidence=0.8,
    )
    assert [row[0].id for row in reranked] == ["p-1"]
    assert counts["rejected"] == 0


def test_evidence_is_compact_and_does_not_include_image_url() -> None:
    evidence = evidence_from_scored(
        [
            _row(
                "p-1",
                0.9,
                {
                    "scene": "户外",
                    "objects": [f"object-{index}" for index in range(30)],
                    "text_in_image": ["A"],
                    "summary": "草地上的狗",
                },
            )
        ],
        1,
    )
    assert evidence[0]["candidate_key"] == "c0"
    assert len(evidence[0]["analysis"]["objects"]) == 20
    assert "image_url" not in evidence[0]


@pytest.mark.asyncio
async def test_verified_pool_can_find_match_beyond_first_top_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def judge(_query, candidates, **_kwargs):
        decisions = []
        for candidate in candidates:
            is_target = candidate["description"] == "description-p-6"
            decisions.append(
                RerankDecision(
                    candidate["candidate_key"],
                    "match" if is_target else "contradiction",
                    0.99,
                    "test",
                )
            )
        return decisions, {"cache_hit": False, "model": "test", "latency_ms": 1.0}

    monkeypatch.setattr(
        "app.services.search_reranker.judge_candidate_evidence", judge
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_enabled", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_top_k", 5
    )
    scored = [_row(f"p-{index}", 1 - index / 100) for index in range(1, 9)]

    result, summary = await verify_scored_candidate_pool(
        scored,
        "切尔西",
        enabled=True,
        max_candidates=8,
        max_results=3,
    )

    assert [row[0].id for row in result] == ["p-6"]
    assert summary["batch_count"] == 2
    assert summary["match_count"] == 1


@pytest.mark.asyncio
async def test_runtime_reranker_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("app.services.search_reranker.judge_candidate_evidence", broken)
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_enabled", True
    )
    scored = [_row("p-1", 0.9), _row("p-2", 0.8)]

    result, summary = await rerank_scored_candidates(
        scored, "query", enabled=True, page_limit=2
    )

    assert result == scored
    assert summary is not None
    assert summary["degraded"] is True
    assert summary["rejected_count"] == 0


@pytest.mark.asyncio
async def test_runtime_reranker_returns_empty_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_match(*args, **kwargs):
        return (
            [
                RerankDecision("c0", "contradiction", 0.95, "wrong object"),
                RerankDecision("c1", "uncertain", 0.6, "insufficient evidence"),
            ],
            {"cache_hit": False, "model": "test", "latency_ms": 1.0},
        )

    monkeypatch.setattr(
        "app.services.search_reranker.judge_candidate_evidence", no_match
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_enabled", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_require_match", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_visual_verify_enabled", False
    )

    result, summary = await rerank_scored_candidates(
        [_row("p-1", 0.9), _row("p-2", 0.8)],
        "query",
        enabled=True,
        page_limit=2,
    )

    assert result == []
    assert summary is not None
    assert summary["zero_match_filtered"] is True


def test_visual_trigger_is_selective(monkeypatch: pytest.MonkeyPatch) -> None:
    decisions = [
        RerankDecision("c0", "match", 0.9, "supported"),
        RerankDecision("c1", "uncertain", 0.5, "not enough"),
    ]
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_visual_verify_score_gap", 0.05
    )
    assert (
        visual_trigger_reason(
            "右侧的孩子在跑",
            [_row("p-1", 0.90), _row("p-2", 0.88)],
            decisions,
            reject_confidence=0.8,
        )
        == "fine_grained_close_scores"
    )
    assert (
        visual_trigger_reason(
            "海边照片",
            [_row("p-1", 0.90), _row("p-2", 0.88)],
            decisions,
            reject_confidence=0.8,
        )
        is None
    )


def test_visual_uncertain_does_not_erase_text_match() -> None:
    merged = merge_visual_decisions(
        [RerankDecision("c0", "match", 0.9, "text")],
        [VisualDecision("c0", "uncertain", 0.7, "blurred")],
        reject_confidence=0.8,
    )
    assert merged[0].verdict == "match"


@pytest.mark.asyncio
async def test_runtime_visual_verifier_recovers_zero_text_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_text_match(*args, **kwargs):
        return (
            [
                RerankDecision("c0", "uncertain", 0.6, "missing action"),
                RerankDecision("c1", "uncertain", 0.6, "missing action"),
            ],
            {"cache_hit": False, "model": "test", "latency_ms": 1.0},
        )

    async def visual_match(*args, **kwargs):
        return (
            [
                VisualDecision("c0", "match", 0.95, "visible action"),
                VisualDecision("c1", "contradiction", 0.95, "wrong action"),
            ],
            {"cache_hit": False, "model": "vl-test", "latency_ms": 2.0},
        )

    monkeypatch.setattr(
        "app.services.search_reranker.judge_candidate_evidence", no_text_match
    )
    monkeypatch.setattr(
        "app.services.search_reranker.judge_visual_candidates", visual_match
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_enabled", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_require_match", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_visual_verify_enabled", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_visual_verify_top_k", 2
    )

    result, summary = await rerank_scored_candidates(
        [_row("p-1", 0.9), _row("p-2", 0.8)],
        "孩子正在跑动",
        enabled=True,
        page_limit=2,
    )

    assert [row[0].id for row in result] == ["p-1"]
    assert summary is not None
    assert summary["visual_verification_applied"] is True
    assert summary["visual_trigger_reason"] == "zero_match_uncertain"
    assert summary["visual_match_count"] == 1


@pytest.mark.asyncio
async def test_runtime_reranker_does_not_backfill_unjudged_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def one_match(*args, **kwargs):
        return (
            [
                RerankDecision("c0", "match", 0.99, "supported"),
                RerankDecision("c1", "contradiction", 0.99, "wrong object"),
            ],
            {"cache_hit": False, "model": "test", "latency_ms": 1.0},
        )

    monkeypatch.setattr(
        "app.services.search_reranker.judge_candidate_evidence", one_match
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_enabled", True
    )
    monkeypatch.setattr(
        "app.services.search_reranker.settings.search_rerank_require_match", True
    )

    result, summary = await rerank_scored_candidates(
        [_row("p-1", 0.9), _row("p-2", 0.8), _row("p-3", 0.7)],
        "query",
        enabled=True,
        page_limit=2,
    )

    assert [row[0].id for row in result] == ["p-1"]
    assert summary is not None
    assert summary["unjudged_filtered_count"] == 1


def test_oracle_replay_keeps_unlabeled_candidates_as_uncertain() -> None:
    query = {
        "id": "q1",
        "query": "猫",
        "relevant_photo_ids": ["p-1"],
        "candidate_judgments": [
            {"photo_id": "p-1", "verdict": "match", "rationale": "cat"},
            {"photo_id": "p-2", "verdict": "contradiction", "rationale": "dog"},
        ],
    }
    baseline, reranked = replay([query], {"q1": ["p-2", "p-3", "p-1"]}, top_k=3)
    assert baseline["q1"] == ["p-2", "p-3", "p-1"]
    assert reranked["q1"] == ["p-1", "p-3"]
    assert not validate_rerank_labels([query])
