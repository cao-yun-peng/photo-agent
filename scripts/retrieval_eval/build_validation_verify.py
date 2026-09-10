"""Read-only integrity and closed-corpus consistency gate; zero provider calls."""
import hashlib
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/eval/retrieval_validation"


def main():
    freeze = json.loads((OUT / "freeze.json").read_text(encoding="utf-8"))
    for name, expected in freeze["files"].items():
        path = (ROOT / name).resolve()
        assert path.is_relative_to(ROOT.resolve()), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    corpus = json.loads((OUT / "corpus.json").read_text(encoding="utf-8"))
    queries = [json.loads(x) for x in (OUT / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
    ids = {p["photo_id"] for p in corpus}
    assert len(ids) == len(corpus) == 40
    assert len({p["sha256"] for p in corpus}) == len({p["database_photo_id"] for p in corpus}) == 40
    assert len({q["id"] for q in queries}) == len(queries) == 80
    for p in corpus:
        path = ROOT / p["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == p["sha256"]
        with Image.open(path) as image:
            assert image.size == (p["width"], p["height"])
            image.verify()
    covered = set()
    for q in queries:
        pos, neg = set(q["relevant_photo_ids"]), set(q["hard_negative_photo_ids"])
        assert pos | neg <= ids and not pos & neg, q["id"]
        assert q["expected_empty"] == (not pos) and q["split"] == "validation", q["id"]
        assert not q["exclude_ids_from_request"] and q["family_id"], q["id"]
        covered |= pos
    assert covered == ids
    expected_qrels = ["query_id\tphoto_id\trelevance"] + [f"{q['id']}\t{p['photo_id']}\t{int(p['photo_id'] in q['relevant_photo_ids'])}" for q in queries for p in corpus]
    assert (OUT / "qrels.tsv").read_text(encoding="utf-8").splitlines() == expected_qrels
    provenance = json.loads((OUT / "provenance.json").read_text(encoding="utf-8"))
    assert {p["photo_id"] for p in provenance} == ids
    assert all(p["source_page"].startswith("https://commons.wikimedia.org/") and p["author"] and p["license"] for p in provenance)
    leakage = json.loads((OUT / "leakage-audit.json").read_text(encoding="utf-8"))
    assert not leakage["exact_cross_duplicates"] and not leakage["near_cross_pairs"] and not leakage["near_within_pairs"]
    assert leakage["cross_pairs"] == 40 * 137
    summary = {"status": "passed", "dataset_version": freeze["version"], "photos": len(corpus), "queries": len(queries), "relevance_pairs": len(corpus) * len(queries),
               "positive_queries": sum(not q["expected_empty"] for q in queries), "empty_queries": sum(q["expected_empty"] for q in queries),
               "multi_positive_queries": sum(len(q["relevant_photo_ids"]) > 1 for q in queries), "families": len({q["family_id"] for q in queries}), "verified_frozen_files": len(freeze["files"]),
               "source_attribution_complete": True, "model_calls": 0}
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
