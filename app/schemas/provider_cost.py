from typing import Any
from pydantic import BaseModel


class ProviderCostOut(BaseModel):
    currency: str
    scope: str
    call_count: int
    estimated_yuan_known: float
    unknown_estimate_count: int
    actual_yuan: float | None
    complete: bool
    billing_status: str
    calls: list[dict[str, Any]]
    generation_id: str | None = None
    scope_note: str | None = None
    truncated: bool = False
    limit: int | None = None
