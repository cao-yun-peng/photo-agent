"""Strict response contract shared only by the search evidence judges.

Accept an object envelope or a complete array; never extract inner objects from
an array or repair a partial batch into successful decisions.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

DECISION_CONTRACT_VERSION = "search_decisions_v2"
_VERDICTS = {"match", "contradiction", "uncertain"}
_JSON_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", re.I | re.S)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-JSON numeric constant: {value}")


def parse_decision_rows(
    payload: Any, expected_keys: set[str] | None
) -> list[dict[str, Any]] | None:
    """Validate every row and exact candidate coverage, or reject the whole batch.

    The runtime always supplies expected keys. The optional form is retained for
    existing pure helper callers. Existing case-insensitive verdicts and rationale
    truncation are preserved; keys and confidence are never coerced or defaulted.
    """
    if isinstance(payload, str):
        text = payload.strip()
        fence = _JSON_FENCE.fullmatch(text)
        if fence:
            text = fence.group(1).strip()
        try:
            # Decode the complete document first, preserving an array envelope.
            payload = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, TypeError):
            return None

    rows = payload.get("decisions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            return None
        key = raw.get("candidate_key")
        verdict = raw.get("verdict")
        confidence = raw.get("confidence")
        rationale = raw.get("rationale")
        if (
            not isinstance(key, str)
            or not key
            or key in seen
            or (expected_keys is not None and key not in expected_keys)
            or not isinstance(verdict, str)
            or verdict.lower() not in _VERDICTS
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
            or not math.isfinite(confidence)
            or not isinstance(rationale, str)
        ):
            return None
        seen.add(key)
        parsed.append(
            {
                "candidate_key": key,
                "verdict": verdict.lower(),
                "confidence": float(confidence),
                "rationale": rationale[:240],
            }
        )
    if expected_keys is not None and seen != expected_keys:
        return None
    return parsed
