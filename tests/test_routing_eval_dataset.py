import json
import re
from collections import Counter
from datetime import date
from pathlib import Path


DATASET_DIR = Path(__file__).parent / "eval" / "routing"
DATASET_PATH = DATASET_DIR / "turn_routing_v1.jsonl"
META_PATH = DATASET_DIR / "turn_routing_v1.meta.json"
SCHEMA_PATH = DATASET_DIR / "turn_routing_v1.schema.json"

INTENTS = {
    "photo_search",
    "search_more",
    "result_feedback",
    "complex_agent",
    "unknown",
}
RELATIONS = {"new", "replace", "refine", "continue", "none"}
SPLITS = {"development", "validation", "test"}
SOURCES = {"rule", "llm"}


def _load_cases() -> list[dict]:
    cases = []
    for line_number, raw_line in enumerate(
        DATASET_PATH.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            cases.append(json.loads(raw_line))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"invalid JSONL at line {line_number}: {exc}") from exc
    return cases


def test_routing_seed_dataset_structure_and_split_counts() -> None:
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    cases = _load_cases()

    assert schema["$id"] == "urn:photo-agent:eval:routing:v1"
    assert meta["annotation_status"] == (
        "single_pass_seed_pending_independent_review"
    )
    assert len(cases) == sum(meta["split_counts"].values()) == 80
    assert Counter(case["split"] for case in cases) == Counter(meta["split_counts"])
    assert len({case["id"] for case in cases}) == len(cases)

    for case in cases:
        assert re.fullmatch(r"route-(dev|val|test)-\d{3}", case["id"])
        assert case["split"] in SPLITS
        assert date.fromisoformat(case["reference_date"])
        assert set(case) <= {
            "id",
            "split",
            "reference_date",
            "context",
            "user_input",
            "expected",
            "tags",
            "risk",
            "notes",
        }
        assert set(case["context"]) == {
            "active_search",
            "recent_messages",
            "last_search_items",
            "confirmed_photo_id",
        }
        assert isinstance(case["user_input"], str)
        assert case["tags"] and len(case["tags"]) == len(set(case["tags"]))
        assert case["risk"] in {"normal", "safety_critical"}

        expected = case["expected"]
        assert expected["rule_outcome"] in {"plan", "defer"}
        assert expected["intent"] in INTENTS
        assert expected["relation"] in RELATIONS
        assert set(expected["allowed_sources"]) <= SOURCES
        assert isinstance(expected["needs_clarification"], bool)

        if expected["rule_outcome"] == "defer":
            assert expected["allowed_sources"] == ["llm"]
            assert "model_required" in case["tags"]
        else:
            assert expected["allowed_sources"] == ["rule"]
            assert "model_required" not in case["tags"]
        if case["risk"] == "safety_critical":
            assert any(
                tag in case["tags"]
                for tag in {"safety", "generation", "delete", "edit", "share"}
            )


def test_routing_seed_dataset_has_required_coverage() -> None:
    cases = _load_cases()
    intents = {case["expected"]["intent"] for case in cases}
    relations = {case["expected"]["relation"] for case in cases}
    tags = {tag for case in cases for tag in case["tags"]}

    assert intents == INTENTS
    assert relations == RELATIONS
    assert {
        "new",
        "replace",
        "refine",
        "continue",
        "result_feedback",
        "selection",
        "vague",
        "lost_reference",
        "model_required",
        "prompt_injection",
        "date",
        "place",
        "visual_date",
    } <= tags
    assert sum(case["expected"]["rule_outcome"] == "defer" for case in cases) >= 12
    assert sum(case["risk"] == "safety_critical" for case in cases) >= 8
