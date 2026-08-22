from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import settings
from app.models.generation import Generation
from app.services.agent import AgentState, PhotoAgent
from app.services.agent_workflow import transition_workflow
from app.services.generation_service import (
    GenerationDomainError,
    confirm_generation,
    generation_confirmation_payload,
    prepare_generation,
)
from app.services.rollout import agent_variant_for_user, rollout_bucket
from app.services.search import (
    complete_scope_is_reliable,
    infer_complete_result_filters,
)
from app.services.turn_resolver import resolve_turn


def test_open_category_is_not_claimed_as_complete_scope() -> None:
    inferred = infer_complete_result_filters("把我拍的鸟全部给我")

    assert not complete_scope_is_reliable(inferred)


def test_structured_selfie_scope_can_be_enumerated() -> None:
    inferred = infer_complete_result_filters("把我的全部自拍给我")

    assert complete_scope_is_reliable(inferred)
    assert inferred["is_selfie"] is True


@pytest.mark.asyncio
async def test_open_complete_search_routes_to_exhaustive_semantic_strategy() -> None:
    plan = await resolve_turn("把我拍的鸟全部给我")

    assert plan.search is not None
    assert plan.search.retrieval_strategy == "exhaustive_semantic"
    assert not plan.can_use_search_fast_path


@pytest.mark.asyncio
async def test_structured_complete_search_keeps_safe_fast_path() -> None:
    plan = await resolve_turn("把我的全部自拍给我")

    assert plan.search is not None
    assert plan.search.retrieval_strategy == "structured_complete"
    assert plan.can_use_search_fast_path


def test_rollout_bucket_is_stable_and_kill_switch_wins(monkeypatch) -> None:
    user_id = uuid4()
    assert rollout_bucket(user_id) == rollout_bucket(user_id)

    monkeypatch.setattr(settings, "agent_v2_enabled", True)
    monkeypatch.setattr(settings, "agent_v2_rollout_percent", 100)
    monkeypatch.setattr(settings, "agent_v2_kill_switch", False)
    assert agent_variant_for_user(user_id) == "v2"

    monkeypatch.setattr(settings, "agent_v2_kill_switch", True)
    assert agent_variant_for_user(user_id) == "control"


def test_workflow_rejects_skipping_user_selection() -> None:
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="找照片并生成",
        workflow_state="awaiting_selection",
    )

    with pytest.raises(ValueError, match="invalid workflow transition"):
        transition_workflow(state, "awaiting_generation_confirmation")

    transition_workflow(state, "selection_confirmed")
    transition_workflow(state, "awaiting_generation_confirmation")
    transition_workflow(state, "generation_queued")


def test_v2_only_exposes_business_level_tools() -> None:
    agent = PhotoAgent(db=None)  # type: ignore[arg-type]
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        original_query="处理照片",
        agent_variant="v2",
    )

    names = {item["function"]["name"] for item in agent._tool_schemas_for_state(state)}

    assert names == {
        "search_photos",
        "ask_clarification",
        "apply_skill",
        "recommend_skills",
    }
    assert "fallback_search" not in names
    assert "browse_candidates" not in names


@pytest.mark.asyncio
async def test_prepare_generation_reuses_idempotent_task() -> None:
    user_id = uuid4()
    existing = Generation(
        id=uuid4(),
        user_id=user_id,
        source_photo_id=uuid4(),
        status="awaiting_confirmation",
        idempotency_key="stable-request-key",
    )
    db = SimpleNamespace(execute=AsyncMock())
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: existing)

    result = await prepare_generation(
        db=db,  # type: ignore[arg-type]
        user_id=user_id,
        photo_id=uuid4(),
        idempotency_key="stable-request-key",
    )

    assert result is existing


@pytest.mark.asyncio
async def test_confirm_generation_is_idempotent_after_queue() -> None:
    user_id = uuid4()
    token = uuid4()
    generation = Generation(
        id=uuid4(),
        user_id=user_id,
        source_photo_id=uuid4(),
        status="pending",
        enqueue_status="queued",
        confirmation_token=token,
    )
    db = SimpleNamespace(execute=AsyncMock())
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: generation)

    result = await confirm_generation(
        db=db,  # type: ignore[arg-type]
        user_id=user_id,
        generation_id=generation.id,
        confirmation_token=token,
    )

    assert result is generation


@pytest.mark.asyncio
async def test_expired_confirmation_never_reserves_or_queues() -> None:
    user_id = uuid4()
    token = uuid4()
    generation = Generation(
        id=uuid4(),
        user_id=user_id,
        source_photo_id=uuid4(),
        status="awaiting_confirmation",
        enqueue_status="not_queued",
        confirmation_token=token,
        confirmation_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: generation)

    with pytest.raises(GenerationDomainError) as exc_info:
        await confirm_generation(
            db=db,  # type: ignore[arg-type]
            user_id=user_id,
            generation_id=generation.id,
            confirmation_token=token,
        )

    assert exc_info.value.code == "confirmation_expired"
    assert generation.status == "failed"
    assert generation.quota_reserved is not True
    db.commit.assert_awaited_once()


def test_confirmation_payload_contains_cost_and_not_prompt() -> None:
    generation = Generation(
        id=uuid4(),
        user_id=uuid4(),
        source_photo_id=uuid4(),
        status="awaiting_confirmation",
        confirmation_token=uuid4(),
        extra_prompt="private instruction",
    )

    payload = generation_confirmation_payload(generation)

    assert payload["generation_id"] == str(generation.id)
    assert "extra_prompt" not in payload
