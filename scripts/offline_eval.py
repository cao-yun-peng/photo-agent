"""离线评估脚本：评估 VL 结构化分析质量。

用法：
    python scripts/offline_eval.py --dataset tests/eval/ground_truth.json

评估维度：
    - 场景分类 Accuracy
    - 物体检测 Precision / Recall
    - OCR Precision / False Positive
    - 解析成功率（ok / fallback）

数据集格式（JSON）：
    [
      {
        "image_url": "https://...",
        "expected": {
          "scene": "户外",
          "objects": ["猫", "草地"],
          "text_in_image": [" cafe"],
          "persons_count": 2
        }
      },
      ...
    ]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from app.services import ai as ai_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    return text.strip().lower()


def _scene_match(actual: str, expected: str) -> bool:
    return _normalize(actual) == _normalize(expected)


def _object_metrics(actual: list[str], expected: list[str]) -> tuple[int, int, int]:
    """返回 (tp, fp, fn)。"""
    actual_set = {_normalize(x) for x in actual}
    expected_set = {_normalize(x) for x in expected}
    tp = len(actual_set & expected_set)
    fp = len(actual_set - expected_set)
    fn = len(expected_set - actual_set)
    return tp, fp, fn


def _ocr_metrics(actual: list[str], expected: list[str]) -> tuple[int, int, int]:
    """OCR 评估：tp=识别且期望有，fp=识别但期望无，fn=期望有但未识别。"""
    actual_set = {_normalize(x) for x in actual if x and x != "none"}
    expected_set = {_normalize(x) for x in expected if x and x != "none"}
    if not expected_set:
        # 期望无文字：任何识别都是 fp
        return 0, len(actual_set), 0
    tp = len(actual_set & expected_set)
    fp = len(actual_set - expected_set)
    fn = len(expected_set - actual_set)
    return tp, fp, fn


async def evaluate_sample(image_url: str, expected: dict[str, Any]) -> dict[str, Any]:
    """评估单张图片。"""
    try:
        analysis = await ai_service.analyze_image(image_url)
    except Exception as exc:  # noqa: BLE001
        logger.error("analyze_image failed | url=%s exc=%s", image_url, exc)
        return {"error": str(exc), "parse_quality": "error"}

    data = analysis.model_dump(exclude_none=True)
    scene_ok = _scene_match(
        data.get("scene", ""), expected.get("scene", "")
    )

    tp_obj, fp_obj, fn_obj = _object_metrics(
        data.get("objects", []), expected.get("objects", [])
    )
    tp_ocr, fp_ocr, fn_ocr = _ocr_metrics(
        data.get("text_in_image", []), expected.get("text_in_image", [])
    )

    actual_persons = data.get("persons", {}).get("count", 0) or 0
    expected_persons = expected.get("persons_count", 0) or 0
    persons_ok = actual_persons == expected_persons

    return {
        "analysis": data,
        "scene_ok": scene_ok,
        "tp_obj": tp_obj,
        "fp_obj": fp_obj,
        "fn_obj": fn_obj,
        "tp_ocr": tp_ocr,
        "fp_ocr": fp_ocr,
        "fn_ocr": fn_ocr,
        "persons_ok": persons_ok,
        "parse_quality": data.get("parse_quality", "unknown"),
    }


async def evaluate_dataset(dataset_path: str) -> dict[str, Any]:
    with open(dataset_path, "r", encoding="utf-8") as f:
        samples = json.load(f)

    results = []
    for sample in samples:
        result = await evaluate_sample(sample["image_url"], sample.get("expected", {}))
        result["image_url"] = sample["image_url"]
        results.append(result)

    total = len(results)
    errors = sum(1 for r in results if "error" in r)
    scene_ok = sum(1 for r in results if r.get("scene_ok"))
    persons_ok = sum(1 for r in results if r.get("persons_ok"))

    tp_obj = sum(r["tp_obj"] for r in results)
    fp_obj = sum(r["fp_obj"] for r in results)
    fn_obj = sum(r["fn_obj"] for r in results)
    obj_precision = tp_obj / (tp_obj + fp_obj) if (tp_obj + fp_obj) else 0.0
    obj_recall = tp_obj / (tp_obj + fn_obj) if (tp_obj + fn_obj) else 0.0

    tp_ocr = sum(r["tp_ocr"] for r in results)
    fp_ocr = sum(r["fp_ocr"] for r in results)
    fn_ocr = sum(r["fn_ocr"] for r in results)
    ocr_precision = tp_ocr / (tp_ocr + fp_ocr) if (tp_ocr + fp_ocr) else 0.0
    ocr_fpr = fp_ocr / total if total else 0.0

    parse_ok = sum(1 for r in results if r.get("parse_quality") == "ok")

    summary = {
        "total": total,
        "errors": errors,
        "scene_accuracy": scene_ok / total if total else 0.0,
        "persons_accuracy": persons_ok / total if total else 0.0,
        "object_precision": obj_precision,
        "object_recall": obj_recall,
        "ocr_precision": ocr_precision,
        "ocr_fpr": ocr_fpr,
        "parse_ok_rate": parse_ok / total if total else 0.0,
    }

    return {"summary": summary, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Agent 离线评估")
    parser.add_argument(
        "--dataset",
        default="tests/eval/ground_truth.json",
        help="ground truth JSON 路径",
    )
    parser.add_argument(
        "--output",
        default="eval_result.json",
        help="评估结果输出路径",
    )
    args = parser.parse_args()

    result = asyncio.run(evaluate_dataset(args.dataset))
    summary = result["summary"]

    print("\n========== 离线评估结果 ==========")
    print(f"样本总数: {summary['total']}")
    print(f"解析异常: {summary['errors']}")
    print(f"场景分类准确率: {summary['scene_accuracy']:.2%}")
    print(f"人数准确率: {summary['persons_accuracy']:.2%}")
    print(f"物体检测 Precision: {summary['object_precision']:.2%}")
    print(f"物体检测 Recall: {summary['object_recall']:.2%}")
    print(f"OCR Precision: {summary['ocr_precision']:.2%}")
    print(f"OCR 误检率: {summary['ocr_fpr']:.2%}")
    print(f"结构化解析成功率: {summary['parse_ok_rate']:.2%}")
    print("==================================\n")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("结果已保存到 %s", args.output)

    # 任一关键指标不达标则返回非 0，便于 CI 拦截
    if summary["scene_accuracy"] < 0.90:
        logger.error("场景分类准确率未达标 (< 90%%)")
        return 1
    if summary["object_precision"] < 0.80:
        logger.error("物体检测 Precision 未达标 (< 80%%)")
        return 1
    if summary["object_recall"] < 0.70:
        logger.error("物体检测 Recall 未达标 (< 70%%)")
        return 1
    if summary["ocr_precision"] < 0.85:
        logger.error("OCR Precision 未达标 (< 85%%)")
        return 1
    if summary["ocr_fpr"] > 0.05:
        logger.error("OCR 误检率过高 (> 5%%)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
