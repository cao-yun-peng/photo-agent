"""Strict executable dataset contracts, independent of production decisions."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchExpected(Strict):
    query_all_terms: list[str] | None = None
    query_any_terms: list[str] | None = None
    query_forbidden_terms: list[str] | None = None
    from_date: str | None = None
    to_date: str | None = None
    place: str | None = None
    result_mode: Literal["browse", "select"] | None = None
    limit: int | None = Field(default=None, ge=1)
    complete_result_set: bool | None = None
    retrieval_strategy: (
        Literal["vector_fast", "structured_complete", "exhaustive_semantic"] | None
    ) = None


class FeedbackExpected(Strict):
    photo_ids: list[str] | None = None
    continue_search: bool | None = None
    search_query_all_terms: list[str] | None = None


class ClarificationExpected(Strict):
    question_all_terms: list[str] | None = None
    minimum_options: int | None = Field(default=None, ge=0)


class RoutingExpected(Strict):
    rule_outcome: Literal["plan", "defer"]
    intent: Literal[
        "photo_search", "search_more", "result_feedback", "complex_agent", "unknown"
    ]
    relation: Literal["new", "replace", "refine", "continue", "none"]
    allowed_sources: list[Literal["rule", "llm"]] = Field(min_length=1)
    needs_clarification: bool
    search: SearchExpected | None = None
    feedback: FeedbackExpected | None = None
    clarification: ClarificationExpected | None = None


class Context(Strict):
    active_search: dict
    recent_messages: list[dict]
    last_search_items: list[dict]
    confirmed_photo_id: str | None


class Case(Strict):
    id: str = Field(min_length=1)
    split: Literal["development", "validation", "test"]
    reference_date: str
    tags: list[str] = Field(min_length=1)
    risk: Literal["normal", "safety_critical"]

    @model_validator(mode="after")
    def valid_date(self):
        date.fromisoformat(self.reference_date)
        return self


class RoutingCase(Case):
    context: Context
    user_input: str
    expected: RoutingExpected
    notes: str | None = None


class ArgumentAssertion(Strict):
    tool: str
    index: int = Field(ge=0)
    fields: dict[str, Any]


class TrajectoryExpected(Strict):
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    before: list[list[str]] = Field(default_factory=list)
    max_tool_calls: int = Field(ge=0)
    state: dict[str, Any] = Field(default_factory=dict)
    arguments: list[ArgumentAssertion] = Field(default_factory=list)
    terminal: Literal["final", "clarify", "error"]
    final_contains: list[str] = Field(default_factory=list)
    simulated_writes: int | None = Field(default=None, ge=0)


class Turn(Strict):
    user_input: str
    decisions: list[dict] = Field(default_factory=list)
    expected: TrajectoryExpected


class TrajectoryCase(Case):
    variant: Literal["control", "v2"]
    initial_state: dict
    tool_fixtures: dict[str, list[dict]]
    turns: list[Turn] = Field(min_length=1, max_length=10)
    max_model_calls: int = Field(ge=1, le=20)
