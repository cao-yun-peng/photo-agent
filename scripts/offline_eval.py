"""Photo Agent 视觉理解（VL）离线评测器。

支持两种输入：
1. 新版 ``photo_manifest.json``：人工复核的一图一条标注，推荐使用；
2. 旧版 ``[{image_url, expected}]``：仅为向后兼容。

本脚本不上传图片。真实 DashScope VL 需要图片 URL，因此 manifest 中必须通过
``--url-prefix`` 拼出模型可访问的 HTTP(S) 地址，或预先在记录中写 ``image_url``。
``--predictions`` 可在不调用模型的情况下复算评分，适合 CI 和标注审计。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.services import ai as ai_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def fuzzy_contains(actual: str, expected: str) -> bool:
    actual_n, expected_n = normalize_text(actual), normalize_text(expected)
    return bool(
        actual_n and expected_n and (actual_n in expected_n or expected_n in actual_n)
    )


def label_hit(
    expected: str, actual_values: list[str], aliases: dict[str, list[str]]
) -> bool:
    candidates = [expected, *aliases.get(expected, [])]
    return any(
        fuzzy_contains(actual, candidate)
        for actual in actual_values
        for candidate in candidates
    )


def _safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return _safe_div(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return _safe_div(self.tp, self.tp + self.fn)


def _manifest_samples(
    payload: dict[str, Any], manifest_path: Path
) -> list[dict[str, Any]]:
    root = (manifest_path.parent / payload.get("image_root", "../..")).resolve()
    samples = []
    for image in payload.get("images", []):
        samples.append(
            {
                "id": image["id"],
                "path": str((root / image["path"]).resolve()),
                "image_url": image.get("image_url"),
                "split": image.get("split", "unspecified"),
                "category": image.get("category", "其他"),
                "expected": image["ground_truth"],
            }
        )
    return samples


def load_samples(dataset_path: str, split: str | None = None) -> list[dict[str, Any]]:
    path = Path(dataset_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "images" in payload:
        samples = _manifest_samples(payload, path)
    elif isinstance(payload, list):
        samples = [
            {
                "id": item.get("id", f"legacy-{index:03d}"),
                "image_url": item["image_url"],
                "path": None,
                "split": item.get("split", "unspecified"),
                "category": item.get("category", "其他"),
                "expected": {
                    "acceptable_scenes": [
                        item.get("expected", {}).get("scene", "其他")
                    ],
                    "required_objects": item.get("expected", {}).get("objects", []),
                    "optional_objects": [],
                    "persons": {
                        "min": item.get("expected", {}).get("persons_count", 0),
                        "max": item.get("expected", {}).get("persons_count", 0),
                    },
                    "required_text": item.get("expected", {}).get("text_in_image", []),
                    "optional_text": [],
                },
            }
            for index, item in enumerate(payload, start=1)
        ]
    else:
        raise ValueError("不支持的数据集格式")
    return [sample for sample in samples if split is None or sample["split"] == split]


def validate_manifest(dataset_path: str) -> dict[str, Any]:
    path = Path(dataset_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "images" not in payload:
        raise ValueError("validate 模式要求新版 photo_manifest.json")
    samples = _manifest_samples(payload, path)
    errors: list[str] = []
    ids, filenames, hashes = set(), set(), set()
    required_gt = {
        "acceptable_scenes",
        "required_objects",
        "optional_objects",
        "persons",
        "required_text",
        "optional_text",
        "summary",
        "search_terms",
    }
    for sample, raw in zip(samples, payload["images"], strict=True):
        for name, value in (
            ("id", raw["id"]),
            ("filename", raw["filename"]),
            ("sha256", raw["sha256"]),
        ):
            target = (
                ids if name == "id" else filenames if name == "filename" else hashes
            )
            if value in target:
                errors.append(f"重复 {name}: {value}")
            target.add(value)
        image_path = Path(sample["path"])
        if not image_path.exists():
            errors.append(f"图片不存在: {raw['id']} -> {image_path}")
        elif raw["filename"] != image_path.name:
            errors.append(f"文件名不一致: {raw['id']}")
        else:
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if digest != raw["sha256"]:
                errors.append(f"图片哈希变化，需要重新复核: {raw['id']}")
            try:
                with Image.open(image_path) as image:
                    if image.size != (raw["width"], raw["height"]):
                        errors.append(
                            f"图片尺寸变化: {raw['id']} expected="
                            f"{raw['width']}x{raw['height']} actual={image.width}x{image.height}"
                        )
                    image.verify()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"图片无法解码: {raw['id']} ({type(exc).__name__}: {exc})"
                )
        missing = required_gt - set(raw.get("ground_truth", {}))
        if missing:
            errors.append(f"{raw['id']} 缺少 ground_truth 字段: {sorted(missing)}")
        persons = raw.get("ground_truth", {}).get("persons", {})
        if persons.get("min", 0) > persons.get("max", 0):
            errors.append(f"{raw['id']} 人数区间非法")
    if payload.get("total_images") != len(samples):
        errors.append("total_images 与实际记录数不一致")
    return {
        "ok": not errors,
        "total": len(samples),
        "splits": {
            split: sum(sample["split"] == split for sample in samples)
            for split in ("development", "validation", "test")
        },
        "errors": errors,
    }


def score_prediction(
    sample: dict[str, Any],
    analysis: dict[str, Any],
    aliases: dict[str, list[str]],
) -> dict[str, Any]:
    expected = sample["expected"]
    scene_actual = str(analysis.get("scene", ""))
    scene_ok = any(
        fuzzy_contains(scene_actual, scene) for scene in expected["acceptable_scenes"]
    )

    actual_objects = [str(value) for value in analysis.get("objects", [])]
    required_objects = [str(value) for value in expected["required_objects"]]
    object_hits = [
        obj for obj in required_objects if label_hit(obj, actual_objects, aliases)
    ]

    actual_text = [str(value) for value in analysis.get("text_in_image", [])]
    required_text = [str(value) for value in expected["required_text"]]
    text_hits = [text for text in required_text if label_hit(text, actual_text, {})]

    persons_value = analysis.get("persons", {})
    actual_persons = int(
        persons_value.get("count", 0) if isinstance(persons_value, dict) else 0
    )
    expected_persons = expected["persons"]
    persons_ok = expected_persons["min"] <= actual_persons <= expected_persons["max"]
    parse_ok = analysis.get("parse_quality", "ok") == "ok"
    return {
        "id": sample["id"],
        "split": sample["split"],
        "category": sample["category"],
        "scene_ok": scene_ok,
        "persons_ok": persons_ok,
        "object_hits": object_hits,
        "object_required": required_objects,
        "object_recall": _safe_div(len(object_hits), len(required_objects)),
        "text_hits": text_hits,
        "text_required": required_text,
        "ocr_recall": _safe_div(len(text_hits), len(required_text)),
        "parse_ok": parse_ok,
        "analysis": analysis,
    }


async def evaluate_sample(image_url: str, expected: dict[str, Any]) -> dict[str, Any]:
    """兼容旧版单样本 API；新代码应使用 manifest + score_prediction。"""
    try:
        analysis_model = await ai_service.analyze_image(image_url)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "parse_quality": "error"}
    analysis = analysis_model.model_dump(exclude_none=True)
    actual_objects = {normalize_text(value) for value in analysis.get("objects", [])}
    expected_objects = {normalize_text(value) for value in expected.get("objects", [])}
    actual_text = {
        normalize_text(value) for value in analysis.get("text_in_image", []) if value
    }
    expected_text = {
        normalize_text(value) for value in expected.get("text_in_image", []) if value
    }
    actual_persons = int(analysis.get("persons", {}).get("count", 0))
    return {
        "analysis": analysis,
        "scene_ok": fuzzy_contains(
            analysis.get("scene", ""), expected.get("scene", "")
        ),
        "tp_obj": len(actual_objects & expected_objects),
        "fp_obj": len(actual_objects - expected_objects),
        "fn_obj": len(expected_objects - actual_objects),
        "tp_ocr": len(actual_text & expected_text),
        "fp_ocr": len(actual_text - expected_text),
        "fn_ocr": len(expected_text - actual_text),
        "persons_ok": actual_persons == int(expected.get("persons_count", 0) or 0),
        "parse_quality": analysis.get("parse_quality", "unknown"),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    valid_results = [result for result in results if "error" not in result]
    ocr_results = [result for result in valid_results if result.get("text_required")]
    summary = {
        "total": total,
        "errors": sum("error" in result for result in results),
        "scene_accuracy": _safe_div(
            sum(bool(result.get("scene_ok")) for result in valid_results), total
        ),
        "persons_accuracy": _safe_div(
            sum(bool(result.get("persons_ok")) for result in valid_results), total
        ),
        "object_macro_recall": _safe_div(
            sum(float(result.get("object_recall", 0)) for result in valid_results),
            total,
        ),
        # 兼容旧报告字段；新版核心物体采用开放词表，只以 Recall 作为正式门禁。
        "object_precision": _safe_div(
            sum(float(result.get("object_recall", 0)) for result in valid_results),
            total,
        ),
        "object_recall": _safe_div(
            sum(float(result.get("object_recall", 0)) for result in valid_results),
            total,
        ),
        "ocr_macro_recall": _safe_div(
            sum(float(result.get("ocr_recall", 0)) for result in ocr_results),
            len(ocr_results),
        ),
        "ocr_samples": len(ocr_results),
        "parse_ok_rate": _safe_div(
            sum(bool(result.get("parse_ok")) for result in valid_results), total
        ),
    }
    summary["gate_passed"] = (
        summary["errors"] == 0
        and summary["scene_accuracy"] >= 0.90
        and summary["persons_accuracy"] >= 0.85
        and summary["object_macro_recall"] >= 0.70
        and summary["ocr_macro_recall"] >= 0.80
        and summary["parse_ok_rate"] >= 0.98
    )
    return summary


async def evaluate_dataset(
    dataset_path: str,
    *,
    split: str | None = None,
    predictions_path: str | None = None,
    url_prefix: str | None = None,
    aliases_path: str | None = None,
) -> dict[str, Any]:
    samples = load_samples(dataset_path, split)
    aliases = (
        json.loads(Path(aliases_path).read_text(encoding="utf-8"))
        if aliases_path and Path(aliases_path).exists()
        else {}
    )
    predictions: dict[str, dict[str, Any]] = {}
    if predictions_path:
        payload = json.loads(Path(predictions_path).read_text(encoding="utf-8"))
        raw_predictions = payload.get("predictions", payload)
        if isinstance(raw_predictions, list):
            predictions = {
                item["id"]: item.get("analysis", item) for item in raw_predictions
            }
        else:
            predictions = raw_predictions

    results = []
    for sample in samples:
        try:
            if predictions_path:
                if sample["id"] not in predictions:
                    raise ValueError("缺少预测结果")
                analysis = predictions[sample["id"]]
            else:
                image_url = sample.get("image_url")
                if not image_url and url_prefix:
                    image_url = f"{url_prefix.rstrip('/')}/{Path(sample['path']).name}"
                if not image_url:
                    raise ValueError("真实 VL 评测需要 image_url 或 --url-prefix")
                parsed = await ai_service.analyze_image(image_url)
                analysis = parsed.model_dump(exclude_none=True)
            results.append(score_prediction(sample, analysis, aliases))
        except Exception as exc:  # noqa: BLE001
            logger.exception("样本 %s 评测失败", sample["id"])
            results.append(
                {"id": sample["id"], "error": f"{type(exc).__name__}: {exc}"}
            )
    return {"summary": summarize(results), "results": results}


def print_summary(summary: dict[str, Any]) -> None:
    print("\n========== VL 图片理解评测 ==========")
    print(f"样本: {summary['total']} | 错误: {summary['errors']}")
    print(f"场景准确率: {summary['scene_accuracy']:.2%}")
    print(f"人数区间准确率: {summary['persons_accuracy']:.2%}")
    print(f"核心物体 Macro Recall: {summary['object_macro_recall']:.2%}")
    print(
        f"OCR Macro Recall: {summary['ocr_macro_recall']:.2%} ({summary['ocr_samples']} 张)"
    )
    print(f"解析成功率: {summary['parse_ok_rate']:.2%}")
    print(f"门禁: {'PASS' if summary['gate_passed'] else 'FAIL'}")
    print("=====================================\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Agent VL 图片理解评测")
    parser.add_argument("--dataset", default="tests/eval/photo_manifest.json")
    parser.add_argument("--output", default="artifacts/vl-eval-result.json")
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("--predictions", help="已有预测 JSON；提供后不调用真实 VL")
    parser.add_argument("--url-prefix", help="图片所在的公开 HTTP(S) 目录")
    parser.add_argument("--aliases", default="tests/eval/object_aliases.json")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        result = validate_manifest(args.dataset)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    output = asyncio.run(
        evaluate_dataset(
            args.dataset,
            split=args.split,
            predictions_path=args.predictions,
            url_prefix=args.url_prefix,
            aliases_path=args.aliases,
        )
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(output["summary"])
    return 0 if output["summary"]["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
