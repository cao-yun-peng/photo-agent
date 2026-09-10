"""Offline, fail-closed reports for the preregistered retrieval experiment.

No application imports, environment reconfiguration, database, or provider calls.
Operational failures are valid observed outcomes; unattempted queries and harness
integrity failures make the comparison incomplete and prohibit selection.
"""

import argparse
import hashlib
import json
import os
import random
import tempfile
from pathlib import Path

from scripts.retrieval_eval.environment import ROOT, EVIDENCE
from scripts.retrieval_eval.metrics import score_results, paired_cluster_bootstrap


DATASETS = {"development": "tests/eval/retrieval_v2", "validation": "tests/eval/retrieval_validation"}
EXPECTED = {"development": (217, 137), "validation": (80, 40)}
LIMITATIONS = [
    "开发集全部为合成图片，不能据此推断真实个人相册上的总体效果。",
    "开发集大量查询只有一个正例；固定分母 Precision@5 的单正例上限为20%，须结合 Recall@5 和正例数量切片。",
    "超时、供应商失败和核验不完整的空结果不计正确拒绝；正例失败仍记录实际返回质量，操作失败单列。",
    "这是生产 SearchService 服务层实验，不能声称真实 HTTP 或微信端 E2E 验证。",
    "费用为保守估算，未与供应商实际账单核对；未知用量保留预留额。",
    "本地冷/热状态依据实际 cache hit/miss；供应商内部缓存及模型别名权重漂移不可据此排除。",
    "标签由 Codex 视觉复核，未经独立人工双标；验证数据独立于开发输入，不保证未进入基础模型训练。",
    "置信区间按显式查询家族整簇配对重采样，不能把相关查询当作独立样本。",
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def write(path, value, *, markdown=False):
    """Atomic current artifact, retaining a hash-named copy of changed history."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = value if markdown else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    raw = content.encode("utf8")
    if path.exists():
        previous = path.read_bytes()
        if previous == raw:
            return
        history = path.parent / "history" / f"{path.stem}-{hashlib.sha256(previous).hexdigest()[:16]}{path.suffix}"
        history.parent.mkdir(exist_ok=True)
        if not history.exists():
            history.write_bytes(previous)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _issue(kind, **details):
    return {"kind": kind, **details}


def _frozen(root, path, required=()):
    issues = []
    try:
        frozen = read(path)
        sources = frozen["sources"]
        if not isinstance(sources, dict):
            raise ValueError("sources must be a mapping")
        for name in required:
            if name not in sources:
                issues.append(_issue("missing_frozen_input", path=name))
        for name, expected in sources.items():
            source = (root / name).resolve()
            if not source.is_relative_to(root.resolve()) or not source.is_file() or digest(source) != expected:
                issues.append(_issue("frozen_input_changed", path=name))
        for key, base in (("app_source_file_set", root / "app"),
                          ("evaluation_source_file_set", root / "scripts/retrieval_eval")):
            if key in frozen:
                files = base.rglob("*.py") if key.startswith("app_") else base.glob("*.py")
                if sorted(p.relative_to(root).as_posix() for p in files) != frozen[key]:
                    issues.append(_issue("frozen_inventory_changed", inventory=key))
        return frozen, digest(path), issues
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {}, None, [_issue("freeze_unavailable_or_invalid", path=str(path), error=type(exc).__name__)]


def _dataset(root, dataset):
    directory = root / DATASETS[dataset]
    issues = []
    try:
        corpus = read(directory / "corpus.json")
        queries = [json.loads(line) for line in (directory / "queries.jsonl").read_text(encoding="utf8").splitlines() if line.strip()]
        expected_queries, expected_photos = EXPECTED[dataset]
        if len(queries) != expected_queries or len(corpus) != expected_photos:
            issues.append(_issue("dataset_size_mismatch", expected_queries=expected_queries,
                                 actual_queries=len(queries), expected_photos=expected_photos, actual_photos=len(corpus)))
        ids = [q["id"] for q in queries]
        if len(ids) != len(set(ids)) or any(not isinstance(qid, str) or Path(qid).name != qid for qid in ids):
            issues.append(_issue("invalid_query_identity"))
        return corpus, queries, issues
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [], [], [_issue("dataset_unavailable_or_invalid", error=type(exc).__name__)]


def _core_config_issues(frozen):
    valid = (type(frozen.get("query_order_seed")) is int
             and isinstance(frozen.get("request"), dict)
             and isinstance(frozen.get("settings"), dict)
             and isinstance(frozen.get("variants"), dict)
             and set(frozen["variants"]) == set("ABCD")
             and all(isinstance(item, dict) for item in frozen["variants"].values()))
    return [] if valid else [_issue("frozen_configuration_incomplete")]


def _load_run(evidence, dataset, variant, queries, corpus, freeze_hash, seed):
    directory = evidence / "runs" / dataset / variant
    issues, rows, hashes = [], [], {}
    if (directory / "harness-failures.json").exists():
        issues.append(_issue("harness_failures", variant=variant))
    ids = [q["id"] for q in queries]
    order = ids.copy()
    random.Random(seed).shuffle(order)
    try:
        schedule = read(directory / "schedule.json")
        expected = {"dataset": dataset, "variant": variant, "freeze_sha256": freeze_hash, "query_ids": order}
        if schedule != expected:
            issues.append(_issue("schedule_mismatch", variant=variant))
        hashes["schedule.json"] = digest(directory / "schedule.json")
    except (OSError, ValueError):
        issues.append(_issue("schedule_missing_or_invalid", variant=variant))
    allowed = {f"{qid}.json" for qid in ids} | {f"{qid}.started.json" for qid in ids} | {"schedule.json", "harness-failures.json"}
    for path in directory.glob("*.json"):
        if path.name not in allowed:
            issues.append(_issue("unexpected_result_file", variant=variant, path=path.name))
    for qid in ids:
        path = directory / f"{qid}.json"
        try:
            row = read(path)
            if not isinstance(row, dict):
                raise ValueError("result must be object")
            hashes[path.name] = digest(path)
            expected = {"query_id": qid, "dataset": dataset, "variant": variant, "freeze_sha256": freeze_hash}
            if any(row.get(key) != value for key, value in expected.items()):
                issues.append(_issue("result_identity_mismatch", query_id=qid, variant=variant))
            if row.get("attempted") is not True:
                issues.append(_issue("query_not_attempted", query_id=qid, variant=variant))
            if "completed_at" not in row:
                issues.append(_issue("result_not_completed", query_id=qid, variant=variant))
            if row.get("invalid_returned_database_ids"):
                issues.append(_issue("out_of_corpus_return", query_id=qid, variant=variant))
            rows.append(row)
        except (OSError, ValueError, TypeError):
            issues.append(_issue("result_missing_or_invalid", query_id=qid, variant=variant))
    score = None
    if not issues:
        try:
            score = score_results(queries, rows, [p["photo_id"] for p in corpus])
            if any(not q.get("family_id") for q in queries):
                issues.append(_issue("missing_query_family"))
        except (KeyError, ValueError, TypeError) as exc:
            issues.append(_issue("invalid_scoring_input", variant=variant, error=type(exc).__name__))
            score = None
    return {"status": "incomplete" if issues else "complete", "issues": issues, "score": score,
            "results_sha256": hashes, "rows": rows}


def _failure_cases(queries, runs, dataset):
    by_id = {query["id"]: query for query in queries}
    cases = []
    for variant, run in runs.items():
        if not run["score"]:
            continue
        for row in run["score"]["per_query"]:
            reasons = []
            if row["operational_error"]:
                reasons.append("operational_error")
            if row["relevant_count"] and row["recall_at_5"] < 1:
                reasons.append("labeled_target_missing_at_5")
            if not row["relevant_count"] and not row["correct_empty"]:
                reasons.append("incorrect_zero_result_answer")
            if row["hard_negative_hit_at_5"]:
                reasons.append("labeled_hard_negative_at_5")
            if row["duplicate_count"]:
                reasons.append("duplicate_return")
            if reasons:
                query = by_id[row["query_id"]]
                top = row["returned_photo_ids"][:5]
                cases.append({"query_id": query["id"], "query": query["query"], "variant": variant,
                              "categories": reasons, "family_id": query["family_id"], "tags": query.get("tags", []),
                              "returned_top5": top, "relevant_photo_ids": query["relevant_photo_ids"],
                              "missing_relevant_at_5": sorted(set(query["relevant_photo_ids"]) - set(top)),
                              "hit_hard_negative_at_5": sorted(set(query["hard_negative_photo_ids"]) & set(top)),
                              "stop_reason": row["stop_reason"], "error_code": row["error_code"], "loss": row["loss"],
                              "evidence": f"runs/{dataset}/{variant}/{query['id']}.json"})
    return cases


def _collect(root, evidence, dataset, variants, frozen, freeze_hash, initial_issues):
    corpus, queries, issues = _dataset(root, dataset)
    issues = initial_issues + issues
    runs = {}
    if not issues:
        for variant in variants:
            runs[variant] = _load_run(evidence, dataset, variant, queries, corpus, freeze_hash, frozen["query_order_seed"])
            issues.extend(runs[variant]["issues"])
    summary = {"status": "incomplete" if issues else "complete", "dataset": dataset,
               "freeze_sha256": freeze_hash, "expected_query_count": EXPECTED[dataset][0],
               "actual_query_count": len(queries), "corpus_size": len(corpus), "issues": issues,
               "variants": {name: {k: value for k, value in run.items() if k != "rows"} for name, run in runs.items()},
               "limitations": LIMITATIONS, "claim_scope": "production_service_layer_not_http_e2e",
               "calls_with_unknown_usage": {name: sum(row.get("calls_with_unknown_usage", 0) for row in run["rows"])
                                              for name, run in runs.items()},
               "cache_observations": {name: _cache_observations(run["rows"]) for name, run in runs.items()}}
    return summary, queries, corpus, runs


def _cache_observations(rows):
    counts = {"measured_hits": 0, "measured_misses": 0, "queries_with_local_hit": 0, "queries_without_cache_telemetry": 0}
    for row in rows:
        usage = (row.get("result_meta") or {}).get("search_usage")
        if not isinstance(usage, dict) or not any(k in usage for k in ("cache_hits", "cache_misses")):
            counts["queries_without_cache_telemetry"] += 1
            continue
        counts["measured_hits"] += usage.get("cache_hits", 0)
        counts["measured_misses"] += usage.get("cache_misses", 0)
        counts["queries_with_local_hit"] += int(usage.get("cache_hits", 0) > 0)
    return counts


def select_configuration(summary, frozen):
    if summary["status"] != "complete" or set(summary["variants"]) != set("ABCD"):
        return {"status": "incomplete", "selected_variant": None, "freeze_sha256": summary["freeze_sha256"],
                "reason": "all_217_queries_must_be_attempted_in_all_four_variants_without_harness_failures"}
    aggregate = {name: item["score"]["aggregate"] for name, item in summary["variants"].items()}
    minimum = min(item["mean_loss"] for item in aggregate.values())
    eligible = [name for name, item in aggregate.items() if item["mean_loss"] <= minimum + .02 + 1e-12]
    selected = min(eligible, key=lambda name: (aggregate[name]["latency_median_ms"],
                                              aggregate[name]["estimated_cost_cny"], aggregate[name]["mean_loss"], name))
    axes = ("mean_loss", "latency_median_ms", "estimated_cost_cny")
    pareto = sorted(name for name, item in aggregate.items() if not any(
        all(other[key] <= item[key] for key in axes) and any(other[key] < item[key] for key in axes)
        for other_name, other in aggregate.items() if other_name != name))
    return {"status": "complete", "selected_variant": selected, "freeze_sha256": summary["freeze_sha256"],
            "selected_variant_config": frozen["variants"][selected], "frozen_request": frozen["request"],
            "frozen_settings": frozen["settings"], "minimum_loss": minimum, "loss_tolerance": .02,
            "eligible_variants": sorted(eligible), "selected_metrics": aggregate[selected],
            "pareto_variants": pareto, "pareto_axes_minimize": list(axes),
            "selection_data": "development_only", "rule": "min_loss_then_within_0.02_median_latency_then_estimated_cost",
            "deterministic_final_tiebreak": "mean_loss_then_variant_name"}


def _markdown(summary, selection=None):
    lines = [f"# {'开发集' if summary['dataset'] == 'development' else '独立验证集'}评测报告草稿", "",
             f"状态：{summary['status']}。固定查询数：{summary['expected_query_count']}。", ""]
    if summary["status"] == "incomplete":
        lines += ["证据不完整，不能选择配置或发布完整对照结论。", "",
                  *[f"- {issue['kind']}：{issue.get('variant', '')} {issue.get('query_id', issue.get('path', ''))}" for issue in summary["issues"]], ""]
    else:
        lines += ["| 配置 | Recall@5 | nDCG@5 | 正确空答案率 | mean loss | 延迟中位数 ms | 估算费用 元 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for name, item in summary["variants"].items():
            a = item["score"]["aggregate"]
            values = [a[k] for k in ("recall_at_5", "ndcg_at_5", "zero_result_accuracy", "mean_loss", "latency_median_ms", "estimated_cost_cny")]
            lines.append("| " + name + " | " + " | ".join("无对应样本" if value is None else f"{value:.4f}" for value in values) + " |")
        if selection:
            lines += ["", f"开发集固定选择：{selection['selected_variant']}。验证结果不用于重新选型。"]
    lines += ["", "## 解释边界", "", *[f"- {text}" for text in LIMITATIONS], "",
              "失败清单只记录观测到的错例类型，不把候选缺失直接归因于某一模型或检索层。根因分析需结合原始核验轨迹复核。", ""]
    return "\n".join(lines)


def development(root=ROOT, evidence=EVIDENCE):
    root, evidence = Path(root), Path(evidence)
    required = [DATASETS["development"] + "/" + name for name in ("corpus.json", "queries.jsonl")]
    frozen, freeze_hash, issues = _frozen(root, evidence / "freeze-v1.json", required)
    issues.extend(_core_config_issues(frozen))
    summary, queries, corpus, runs = _collect(root, evidence, "development", "ABCD", frozen, freeze_hash, issues)
    selection = select_configuration(summary, frozen)
    paired = {"status": summary["status"], "comparisons": {}}
    if summary["status"] == "complete":
        for variant in "BCD":
            paired["comparisons"][f"{variant}_minus_A"] = paired_cluster_bootstrap(
                queries, runs["A"]["rows"], runs[variant]["rows"], [p["photo_id"] for p in corpus])
    directory = evidence / "report"
    write(directory / "development-summary.json", summary)
    selection["development_summary_sha256"] = digest(directory / "development-summary.json")
    write(directory / "selection.json", selection)
    write(directory / "paired-bootstrap.json", paired)
    write(directory / "failures.json", {"status": summary["status"], "cases": _failure_cases(queries, runs, "development")})
    write(directory / "development-report.md", _markdown(summary, selection if selection["status"] == "complete" else None), markdown=True)
    return summary


def validation(root=ROOT, evidence=EVIDENCE):
    root, evidence = Path(root), Path(evidence)
    frozen, freeze_hash, issues = _frozen(root, evidence / "freeze-v1.json")
    issues.extend(_core_config_issues(frozen))
    selection = {}
    try:
        selection = read(evidence / "report/selection.json")
        if not isinstance(selection, dict):
            raise ValueError("selection must be an object")
        if selection.get("status") != "complete" or selection.get("freeze_sha256") != freeze_hash or selection.get("selected_variant") not in ("A", "B", "C", "D"):
            raise ValueError("invalid development selection")
        if digest(evidence / "report/development-summary.json") != selection["development_summary_sha256"]:
            raise ValueError("development summary changed")
    except (OSError, ValueError, KeyError, TypeError):
        selection = {}
        issues.append(_issue("valid_development_selection_required"))
    required = [DATASETS["validation"] + "/" + name for name in ("corpus.json", "queries.jsonl")]
    vf, validation_hash, validation_issues = _frozen(root, evidence / "validation-freeze.json", required)
    issues.extend(validation_issues)
    if vf.get("selected_variant") != selection.get("selected_variant") or not selection.get("selected_variant"):
        issues.append(_issue("validation_selection_mismatch"))
    if vf.get("development_freeze_sha256") != freeze_hash:
        issues.append(_issue("validation_development_freeze_mismatch"))
    if selection and vf.get("selection_sha256") != digest(evidence / "report/selection.json"):
        issues.append(_issue("validation_selection_file_changed"))
    selected = selection.get("selected_variant")
    variants = list(dict.fromkeys(["A", selected])) if selected in ("A", "B", "C", "D") else ["A"]
    summary, queries, corpus, runs = _collect(root, evidence, "validation", variants, frozen, freeze_hash, issues)
    summary["validation_freeze_sha256"] = validation_hash
    summary["selected_variant"] = selected
    summary["selection_sha256"] = digest(evidence / "report/selection.json") if selection else None
    paired = {"status": summary["status"], "comparison": None}
    if summary["status"] == "complete":
        paired["comparison"] = paired_cluster_bootstrap(queries, runs["A"]["rows"], runs[selected]["rows"], [p["photo_id"] for p in corpus])
    directory = evidence / "report"
    write(directory / "validation-summary.json", summary)
    write(directory / "validation-paired-bootstrap.json", paired)
    write(directory / "validation-failures.json", {"status": summary["status"], "cases": _failure_cases(queries, runs, "validation")})
    write(directory / "validation-report.md", _markdown(summary, selection if summary["status"] == "complete" else None), markdown=True)
    return summary


def audit(root=ROOT, evidence=EVIDENCE):
    """Recompute both immutable-input reports; validation never alters selection."""
    dev = development(root, evidence)
    val = validation(root, evidence)
    result = {"status": "complete" if dev["status"] == val["status"] == "complete" else "incomplete",
              "development": dev["status"], "validation": val["status"],
              "issues": dev["issues"] + val["issues"]}
    write(Path(evidence) / "report/audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("development", "validation", "audit"))
    args = parser.parse_args()
    result = globals()[args.action]()
    print(json.dumps({"status": result["status"], "report_directory": str(EVIDENCE / "report")}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "complete" else 2)


if __name__ == "__main__":
    main()
