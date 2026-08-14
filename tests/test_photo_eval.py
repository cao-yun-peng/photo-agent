"""图片清单、VL 评分和检索评分的回归测试。"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from PIL import Image

from scripts.agent_eval import build_real_photo_library
from scripts.import_photo_eval_dataset import SourcePhoto, build_eval_oss_key, load_manifest
from scripts.offline_eval import (
    fuzzy_contains,
    score_prediction,
    summarize,
    validate_manifest,
)
from scripts.retrieval_eval import evaluate, validate_queries


def test_fuzzy_contains_ignores_case_spacing_and_symbols() -> None:
    assert fuzzy_contains("WORLD'S BEST BOSS", "worlds best boss")
    assert fuzzy_contains("中 山 路", "中山路")


def test_vl_scoring_supports_person_range_and_required_ocr() -> None:
    sample = {
        "id": "p-x",
        "split": "test",
        "category": "群像",
        "expected": {
            "acceptable_scenes": ["餐厅", "室内"],
            "required_objects": ["人物", "圆桌"],
            "optional_objects": [],
            "persons": {"min": 8, "max": 12},
            "required_text": ["福"],
            "optional_text": [],
        },
    }
    result = score_prediction(
        sample,
        {
            "scene": "餐厅",
            "objects": ["人", "圆桌"],
            "persons": {"count": 11},
            "text_in_image": ["福"],
            "parse_quality": "ok",
        },
        {"人物": ["人"]},
    )
    assert result["scene_ok"]
    assert result["persons_ok"]
    assert result["object_recall"] == 1.0
    assert result["ocr_recall"] == 1.0


def test_vl_runtime_error_cannot_pass_gate() -> None:
    summary = summarize([{"id": "bad", "error": "network"}])
    assert summary["errors"] == 1
    assert not summary["gate_passed"]


def test_retrieval_metrics_and_negative_query() -> None:
    queries = [
        {"id": "q1", "query": "猫", "relevant_photo_ids": ["p-1"]},
        {
            "id": "q2",
            "query": "北极熊",
            "relevant_photo_ids": [],
            "must_return_empty": True,
        },
    ]
    output = evaluate(queries, {"q1": ["p-2", "p-1"], "q2": []}, k=5)
    assert output["summary"]["recall_at_k"] == 1.0
    assert output["summary"]["mrr"] == 0.5
    assert output["summary"]["empty_query_accuracy"] == 1.0


def test_checked_in_datasets_are_consistent() -> None:
    root = Path(__file__).parents[1]
    manifest_path = root / "tests/eval/photo_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["total_images"] == 112
    assert len(manifest["images"]) == 112
    assert all(
        image["review"]["status"] == "human_reviewed" for image in manifest["images"]
    )

    query_payload = json.loads(
        (root / "tests/eval/retrieval_queries.json").read_text(encoding="utf-8")
    )
    assert not validate_queries(query_payload["queries"], str(manifest_path))
    library = build_real_photo_library(str(manifest_path))
    assert len(library["photos"]) == 112


def test_manifest_validation_detects_changed_image(tmp_path: Path) -> None:
    image_path = tmp_path / "test_photos/p-x.jpg"
    image_path.parent.mkdir()
    image_path.write_bytes(b"not-an-image")
    manifest_path = tmp_path / "tests/eval/photo_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "total_images": 1,
                "image_root": "../..",
                "images": [
                    {
                        "id": "p-x",
                        "filename": "p-x.jpg",
                        "path": "test_photos/p-x.jpg",
                        "sha256": "0" * 64,
                        "width": 1,
                        "height": 1,
                        "ground_truth": {
                            "acceptable_scenes": ["其他"],
                            "required_objects": [],
                            "optional_objects": [],
                            "persons": {"min": 0, "max": 0},
                            "required_text": [],
                            "optional_text": [],
                            "summary": "损坏图片",
                            "search_terms": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = validate_manifest(str(manifest_path))
    assert not result["ok"]
    assert any("哈希变化" in error for error in result["errors"])
    assert any("无法解码" in error for error in result["errors"])


def test_import_loader_and_eval_oss_key(tmp_path: Path) -> None:
    image_path = tmp_path / "test_photos/p-1.jpg"
    image_path.parent.mkdir()
    Image.new("RGB", (8, 6), "red").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "tests/eval/photo_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "total_images": 1,
                "image_root": "../..",
                "images": [
                    {
                        "id": "p-1",
                        "path": "test_photos/p-1.jpg",
                        "sha256": digest,
                        "width": 8,
                        "height": 6,
                        "split": "test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _, photos = load_manifest(manifest_path)
    assert len(photos) == 1
    assert photos[0].mime_type == "image/jpeg"
    assert build_eval_oss_key("user-1", photos[0]) == (
        f"photos/user-1/eval/photo-manifest-v2/{digest}.jpg"
    )


def test_eval_oss_key_uses_content_mime_not_misleading_suffix(tmp_path: Path) -> None:
    misleading_path = tmp_path / "looks-like-jpeg.jpg"
    Image.new("RGB", (2, 2), "blue").save(misleading_path, format="PNG")
    source = SourcePhoto(
        dataset_id="p-mime",
        path=misleading_path,
        sha256="a" * 64,
        size_bytes=misleading_path.stat().st_size,
        mime_type="image/png",
        width=2,
        height=2,
        split="test",
    )
    assert build_eval_oss_key("user-1", source).endswith(".png")
