"""Offline dataset integrity checks and ranked retrieval scoring; no provider calls."""
import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate(dataset):
    meta = json.loads((dataset / "dataset.meta.json").read_text(encoding="utf-8"))
    for section in ("source_sha256", "artifact_sha256"):
        for name, expected in meta[section].items():
            path = ROOT / name if name.startswith("tests/") else dataset / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Hash mismatch: {name}")
    corpus = json.loads((dataset / "corpus.json").read_text(encoding="utf-8"))
    ids = {p["photo_id"] for p in corpus}
    if len(ids) != len(corpus) or len({p["database_photo_id"] for p in corpus}) != len(corpus):
        raise ValueError("Duplicate corpus identity")
    for photo in corpus:
        if hashlib.sha256((ROOT / photo["path"]).read_bytes()).hexdigest() != photo["sha256"]:
            raise ValueError(f"Photo hash mismatch: {photo['photo_id']}")
    queries = read_jsonl(dataset / "queries.jsonl")
    if len({q["id"] for q in queries}) != len(queries):
        raise ValueError("Duplicate query id")
    expected_qrels = []
    for q in queries:
        positive, negative = set(q["relevant_photo_ids"]), set(q["hard_negative_photo_ids"])
        if not (positive | negative) <= ids or positive & negative:
            raise ValueError(f"Invalid labels: {q['id']}")
        if q["expected_empty"] != (not positive) or q["split"] != "development" or q["exclude_ids_from_request"]:
            raise ValueError(f"Invalid evaluation scope: {q['id']}")
        expected_qrels.extend(f"{q['id']}\t0\t{i}\t{int(i in positive)}" for i in sorted(ids))
    if (dataset / "qrels.tsv").read_text(encoding="utf-8").splitlines() != expected_qrels:
        raise ValueError("Qrels disagree with queries")
    if len(corpus) != meta["corpus_size"] or len(queries) != meta["query_count"]:
        raise ValueError("Metadata counts disagree")
    return meta, corpus, queries


def score(queries, results, corpus):
    """Require complete runs. Duplicate results consume ranks, never earn extra credit."""
    by_id = {q["id"]: q for q in queries}
    if len({r["query_id"] for r in results}) != len(results) or {r["query_id"] for r in results} != set(by_id):
        raise ValueError("Results must contain every query exactly once")
    mapping = {p["database_photo_id"]: p["photo_id"] for p in corpus}
    mapping.update({p["photo_id"]: p["photo_id"] for p in corpus})
    details = []
    for result in results:
        q = by_id[result["query_id"]]
        raw = result["photo_ids"]
        if not isinstance(raw, list) or any(not isinstance(i, str) or i not in mapping for i in raw):
            raise ValueError(f"Unknown/out-of-corpus photo id: {q['id']}")
        ranked = [mapping[i] for i in raw]
        positive = set(q["relevant_photo_ids"])
        row = {"query_id": q["id"], "slice": q["tags"][0], "duplicate_count": len(ranked) - len(set(ranked)),
               "hard_negative_hit_at_10": int(bool(set(ranked[:10]) & set(q["hard_negative_photo_ids"])))}
        if not positive:
            row["empty_accuracy"] = int(not ranked)
        else:
            for k in (1, 5, 10):
                hits = len(set(ranked[:k]) & positive)
                row[f"recall_at_{k}"] = hits / len(positive)
                row[f"precision_at_{k}"] = hits / k
            seen, gain, reciprocal = set(), 0.0, 0.0
            for rank, photo_id in enumerate(ranked[:10], 1):
                if photo_id in positive and photo_id not in seen:
                    gain += 1 / math.log2(rank + 1)
                    reciprocal = reciprocal or 1 / rank
                seen.add(photo_id)
            row["mrr_at_10"] = reciprocal
            row["ndcg_at_10"] = gain / sum(1 / math.log2(i + 1) for i in range(1, min(10, len(positive)) + 1))
        if "latency_ms" in result:
            latency = result["latency_ms"]
            if isinstance(latency, bool) or not isinstance(latency, (float, int)) or not math.isfinite(latency) or latency < 0:
                raise ValueError("latency_ms must be finite and nonnegative")
            row["latency_ms"] = latency
        details.append(row)

    def aggregate(rows):
        keys = sorted({k for r in rows for k in r} - {"query_id", "slice"})
        return {k: {"mean": sum(r[k] for r in rows if k in r) / sum(k in r for r in rows),
                    "count": sum(k in r for r in rows)} for k in keys}

    return {"query_count": len(details), "overall": aggregate(details),
            "by_slice": {s: aggregate([r for r in details if r["slice"] == s]) for s in sorted({r["slice"] for r in details})},
            "details": details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "tests/eval/retrieval")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    meta, corpus, queries = validate(args.dataset)
    report = {"dataset_version": meta["version"], "release_gate_eligible": False,
              "mode": "dataset_validation", "photos": len(corpus), "queries": len(queries)}
    if args.results:
        report.update(score(queries, read_jsonl(args.results), corpus))
        report["mode"] = "offline_scoring"
        report["results_sha256"] = hashlib.sha256(args.results.read_bytes()).hexdigest()
    report["dataset_meta_sha256"] = hashlib.sha256((args.dataset / "dataset.meta.json").read_bytes()).hexdigest()
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
