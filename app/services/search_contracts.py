"""Internal immutable plans; adapters never accept a client-authored plan."""

import hashlib
import json
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from app.schemas.photo import SearchQuery


class SearchError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


class SearchRequest(SearchQuery):
    exclude_photo_ids: list[UUID] = Field(default_factory=list)
    verified_only: bool = False
    candidate_pool_size: int = Field(default=12, ge=1, le=100)
    force_visual_verify: bool = False
    include_index_coverage: bool = False
    retrieval_mode: Literal["semantic", "timeline", "album"] = "semantic"


class QueryPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = "search-plan-v3"
    id: UUID
    user_id: UUID
    raw_query: str
    effective_query: str
    timezone: str
    local_date: date
    scoring_time: str
    request_json: str
    parsed_json: str | None = None
    fingerprint: str
    verification: Literal["off", "strict", "soft"]
    allow_visual: bool
    budget_id: UUID
    expires_at: float

    def request(self) -> SearchRequest:
        return SearchRequest.model_validate_json(self.request_json)


def request_fingerprint(request: SearchRequest, timezone: str) -> str:
    values = request.model_dump(
        mode="json",
        exclude={
            "cursor",
            "limit",
            "exclude_photo_ids",
            "include_index_coverage",
            "candidate_pool_size",
            "verified_only",
            "force_visual_verify",
        },
    )
    raw = json.dumps({"request": values, "timezone": timezone}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()
