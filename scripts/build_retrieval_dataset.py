"""Build a development retrieval set from visually authored queries and frozen account hashes.

Offline only. Never reads search outputs, AI descriptions, credentials or a database.
"""
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/eval/retrieval"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build():
    manifest = ROOT / "tests/eval/photo_manifest.json"
    photos = json.loads(manifest.read_text(encoding="utf-8"))["images"]
    account = json.loads((OUT / "account_snapshot.json").read_text(encoding="utf-8"))
    by_hash = {p["hash"]: p for p in account}
    assert len(by_hash) == len(photos) == 137
    corpus, source = [], {p["id"]: p for p in photos}
    for p in photos:
        path = ROOT / p["path"]
        assert sha(path) == p["sha256"] and p["sha256"] in by_hash, p["id"]
        live = by_hash[p["sha256"]]
        corpus.append({"photo_id": p["id"], "database_photo_id": live["id"],
                       "sha256": p["sha256"], "path": p["path"], "width": p["width"], "height": p["height"],
                       "source": p["source"], "source_vl_split": p["split"],
                       "group_id": p.get("group_id", p["id"]),
                       "robustness_slice": int(p["id"][2:]) >= 139,
                       "taken_at": live["taken_at"], "location": live["location"],
                       "index_available_at_snapshot": live["indexed"]})
    assert set(by_hash) == {p["sha256"] for p in corpus}
    write_json(OUT / "corpus.json", corpus)
    ids = {p["photo_id"] for p in corpus}
    queries = []

    def add(kind, query, positive, negatives=None, text=None):
        assert set(positive) <= ids and set(negatives or []) <= ids
        assert not set(positive) & set(negatives or [])
        if negatives is None:
            # Candidate hard negatives only: ranking-independent lexical overlap of legacy labels.
            terms = {t for i in positive for t in source[i]["ground_truth"]["required_objects"]}
            ranked = sorted((i for i in ids if i not in positive), key=lambda i: (
                -len(terms & set(source[i]["ground_truth"]["required_objects"])), i))
            negatives = [i for i in ranked if terms & set(source[i]["ground_truth"]["required_objects"])][:4]
        slices = [kind]
        if any(int(i[2:]) >= 139 for i in positive + negatives):
            slices.append("synthetic_robustness")
        if any(i in {"p-162", "p-163"} for i in positive + negatives):
            slices.append("near_duplicate")
        queries.append({"id": f"recall-{len(queries)+1:03d}", "split": "development", "query": query,
                        "tags": slices, "relevant_photo_ids": positive, "hard_negative_photo_ids": negatives,
                        "expected_empty": not positive, "required_visible_text": text or [],
                        "judgment_status": "codex_draft_pending_independent_review",
                        "judgment_scope": "closed_corpus_provisional",
                        "relevance_rule": "全部明确条件满足才相关；视觉语义近似但违反条件为不相关。",
                        "evidence_photo_ids": sorted(set(positive + negatives)),
                        "source": "visually_authored" if kind != "ocr" else "visible_text_review",
                        "exclude_ids_from_request": [],
                        "notes": "hard_negative是标注，禁止作为检索请求的预先排除项；不得把标签注入被测模型。"})

    with (OUT / "visual_queries.tsv").open(encoding="utf-8", newline="") as f:
        visual = list(csv.DictReader(f, delimiter="\t"))
    assert {x["photo_id"] for x in visual} == ids and len(visual) == len(ids)
    for row in visual:
        add("visual_semantic", row["query"], [row["photo_id"]])
    challenge = json.loads((OUT / "challenge_queries.json").read_text(encoding="utf-8"))
    for query, positive, negative in challenge["paraphrases"]:
        add("colloquial", query, positive, negative)
    for query, positive, text, negative in challenge["ocr"]:
        add("ocr", query, positive, negative, text)
    for query, positive, negative in challenge["sets"]:
        add("multi_positive_or_exclusion", query, positive, negative)
    for query, positive, negative in challenge["zero_result"]:
        add("zero_result", query, positive, negative)
    assert len({q["query"] for q in queries}) == len(queries)
    (OUT / "queries.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False)+"\n" for q in queries), encoding="utf-8")
    with (OUT / "queries.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "query", "tags", "relevant_photo_ids", "hard_negative_photo_ids", "required_visible_text", "review_status"])
        for q in queries:
            writer.writerow([q["id"], q["query"], ";".join(q["tags"]), ";".join(q["relevant_photo_ids"]), ";".join(q["hard_negative_photo_ids"]), ";".join(q["required_visible_text"]), q["judgment_status"]])
    # Fully explicit provisional binary judgments; no silent unjudged-as-negative evaluator default.
    (OUT / "qrels.tsv").write_text("".join(f"{q['id']}\t0\t{i}\t{int(i in q['relevant_photo_ids'])}\n" for q in queries for i in sorted(ids)), encoding="utf-8")
    write_json(OUT / "dataset.meta.json", {
        "schema_version": 1, "dataset_id": "photo-agent-recall-test-user", "version": "1.0.0-development",
        "created_at": "2026-09-07", "account_label": "Photo Eval Dataset v2",
        "account_user_id": "0ba7ad31-c0eb-4483-8ede-037833819759", "corpus_size": len(corpus), "query_count": len(queries),
        "primary_slice_counts": dict(Counter(q["tags"][0] for q in queries)),
        "positive_photo_coverage": len({i for q in queries for i in q["relevant_photo_ids"]}),
        "split_counts": {"development": len(queries), "validation": 0, "test": 0},
        "annotation_status": "Codex视觉复核草案；继承旧VL标注作为辅助，未将其人工复核状态转授新查询标签。",
        "corpus_scope": "137张合成照片，包括25张拟真困难图；不代表真实个人相册分布。",
        "excluded_evaluations": ["拍摄日期/地理位置过滤：所有对应元数据为空", "跨用户隔离：另由安全测试覆盖"],
        "release_gate_eligible": False,
        "leakage_policy": "全部Development。原VL split只留作来源，不继承为L3盲测。近重复图、同义问法与共享图片的查询须成组划分后才建立独立Validation/Test。",
        "source_sha256": {"tests/eval/photo_manifest.json": sha(manifest), "account_snapshot.json": sha(OUT/"account_snapshot.json"),
                          "visual_queries.tsv": sha(OUT/"visual_queries.tsv"), "challenge_queries.json": sha(OUT/"challenge_queries.json")},
        "artifact_sha256": {name: sha(OUT/name) for name in ("corpus.json", "queries.jsonl", "qrels.tsv", "queries.csv")}})
    return {"photos": len(corpus), "queries": len(queries), "slices": dict(Counter(q["tags"][0] for q in queries))}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))
