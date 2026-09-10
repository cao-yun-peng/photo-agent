"""Build immutable validation inputs from explicit pre-outcome visual judgments."""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/eval/retrieval_validation"
VERSION = "1.0.1-independent-real-commons"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def plain(value):
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()


def perceptual_hash(path):
    """64-bit DCT perceptual hash; numpy-only reference implementation."""
    with Image.open(path) as image:
        values = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    n = np.arange(32)
    k = np.arange(8)[:, None]
    basis = np.cos(np.pi / 32 * (n + 0.5) * k)
    coefficients = (basis @ values @ basis.T).reshape(-1)
    bits = coefficients > np.median(coefficients[1:])
    bits[0] = False
    return int("".join("1" if bit else "0" for bit in bits), 2)


def main():
    annotations = json.loads((OUT / "annotations.json").read_text(encoding="utf-8"))
    if annotations["retrieval_outcomes_observed"]:
        raise ValueError("Validation labels cannot be frozen after observing retrieval outcomes")
    corpus, provenance = [], []
    for entry in annotations["images"]:
        selected = OUT / "candidates" / entry["candidate"]
        source = json.loads(selected.with_suffix(selected.suffix + ".json").read_text(encoding="utf-8"))
        info = source["commons_page"]["imageinfo"][0]
        metadata = info["extmetadata"]
        photo_id = entry["photo_id"]
        target = OUT / "images" / (photo_id + selected.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or sha(target) != sha(selected):
            shutil.copyfile(selected, target)
        with Image.open(target) as image:
            width, height = image.size
        corpus.append({
            "photo_id": photo_id,
            "database_photo_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "photo-agent-validation-v1/" + photo_id + "/" + sha(target))),
            "sha256": sha(target), "path": target.relative_to(ROOT).as_posix(),
            "width": width, "height": height, "source": "wikimedia_commons_real_photo",
            "source_vl_split": None, "group_id": entry.get("group_id", photo_id),
            "robustness_slice": False, "taken_at": None, "location": None,
            "index_available_at_snapshot": False,
        })
        provenance.append({
            "photo_id": photo_id, "commons_title": source["commons_page"]["title"],
            "source_page": info["descriptionurl"], "original_image_url": info["url"].split("?")[0],
            "downloaded_image_url": source["download_url"],
            "author": plain(metadata.get("Artist", {}).get("value", "")),
            "credit": plain(metadata.get("Credit", {}).get("value", "")),
            "license": metadata.get("LicenseShortName", {}).get("value", ""),
            "license_url": metadata.get("LicenseUrl", {}).get("value", ""),
            "attribution_required": metadata.get("AttributionRequired", {}).get("value", ""),
            "source_api_path": str(selected.with_suffix(selected.suffix + ".json").relative_to(ROOT)).replace("\\", "/"),
            "sha256": sha(target), "width": width, "height": height,
            "transformation": "Unmodified bytes of Wikimedia-provided raster thumbnail; original image URL retained.",
            "sampling_concept": source["concept"], "candidate_rank": source["rank"],
            "visual_observation": entry["observation"], "selection_reason": entry.get("selection_reason", "First suitable real photograph for the predeclared concept after visual review"),
        })
    ids = {p["photo_id"] for p in corpus}
    assert len(ids) == len(corpus) == 40
    queries = []
    seen = set()
    for q in annotations["queries"]:
        assert q["id"] not in seen
        seen.add(q["id"])
        relevant = q["relevant_photo_ids"]
        negatives = q.get("hard_negative_photo_ids", [])
        assert set(relevant) <= ids and set(negatives) <= ids
        assert not set(relevant) & set(negatives)
        assert len(relevant) == len(set(relevant)) and len(negatives) == len(set(negatives))
        queries.append({
            "id": q["id"], "split": "validation", "query": q["query"], "tags": q["tags"],
            "relevant_photo_ids": relevant, "hard_negative_photo_ids": negatives,
            "expected_empty": not relevant, "required_visible_text": q.get("required_visible_text", []),
            "family_id": q["family_id"], "judgment_status": "codex_visual_reviewed_before_retrieval",
            "judgment_scope": "exhaustive_closed_corpus_visual_review",
            "relevance_rule": "Every explicit visible condition must hold; merely related subject with contradictory OCR/action/object is irrelevant. Unstated location/time/identity is not inferred.",
            "evidence_photo_ids": sorted(set(relevant + negatives)), "source": "visually_authored_real_photo_validation",
            "exclude_ids_from_request": [], "notes": q.get("notes", "Ground-truth positives and hard negatives are scoring-only and must not enter model requests."),
        })
    assert len(queries) == 80
    write("corpus.json", corpus)
    write("provenance.json", provenance)
    (OUT / "queries.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in queries), encoding="utf-8")
    (OUT / "qrels.tsv").write_text("query_id\tphoto_id\trelevance\n" + "".join(f"{q['id']}\t{p['photo_id']}\t{int(p['photo_id'] in q['relevant_photo_ids'])}\n" for q in queries for p in corpus), encoding="utf-8")
    development = json.loads((ROOT / "tests/eval/retrieval_v2/corpus.json").read_text(encoding="utf-8"))
    dev_hash = {p["photo_id"]: perceptual_hash(ROOT / p["path"]) for p in development}
    val_hash = {p["photo_id"]: perceptual_hash(ROOT / p["path"]) for p in corpus}
    exact = [{"validation": v["photo_id"], "development": d["photo_id"]} for v in corpus for d in development if v["sha256"] == d["sha256"]]
    near = [{"validation": v, "development": d, "hamming": (vh ^ dh).bit_count()} for v, vh in val_hash.items() for d, dh in dev_hash.items() if (vh ^ dh).bit_count() <= 8]
    within = [{"left": a, "right": b, "hamming": (val_hash[a] ^ val_hash[b]).bit_count()} for a in val_hash for b in val_hash if a < b and (val_hash[a] ^ val_hash[b]).bit_count() <= 8]
    nearest = [{"validation": v, "nearest_development": sorted(({"photo_id": d, "hamming": (vh ^ dh).bit_count()} for d, dh in dev_hash.items()), key=lambda x: x["hamming"])[:3]} for v, vh in val_hash.items()]
    write("leakage-audit.json", {"algorithm": "64-bit low-frequency 8x8 DCT of 32x32 luminance; DC zeroed; median of 63 AC coefficients", "near_duplicate_threshold": 8,
          "development_images": len(development), "validation_images": len(corpus), "cross_pairs": len(development) * len(corpus),
          "exact_cross_duplicates": exact, "near_cross_pairs": near, "near_within_pairs": within, "nearest_cross_pairs": nearest,
          "visual_pair_review": annotations.get("leakage_pair_review", []),
          "limitations": "pHash is a duplicate screen, not proof of absence of every crop, composition or foundation-training overlap."})
    if exact:
        raise ValueError("Exact development image leakage")
    reviewed_pairs = {(x["left"], x["right"]) for x in annotations.get("leakage_pair_review", [])}
    for pair in near:
        if (pair["validation"], pair["development"]) not in reviewed_pairs:
            raise ValueError(f"Unreviewed cross-corpus pHash alert: {pair}")
    for pair in within:
        if (pair["left"], pair["right"]) not in reviewed_pairs:
            raise ValueError(f"Unreviewed within-corpus pHash alert: {pair}")
    now = annotations["reviewed_at"]
    write("review.json", {"reviewer": "Codex AI visual reviewer, not human double annotation", "reviewed_at": now,
          "retrieval_outputs_seen": False, "image_count": len(corpus), "query_count": len(queries),
          "binary_pair_count": len(corpus) * len(queries), "method": annotations["review_method"],
          "images": annotations["images"], "pre_execution_changes": annotations.get("pre_execution_changes", []), "limitations": annotations["limitations"]})
    tags = sorted({tag for q in queries for tag in q["tags"]})
    write("meta.json", {"version": VERSION, "split": "validation", "photos": len(corpus), "queries": len(queries),
          "positive_queries": sum(bool(q["relevant_photo_ids"]) for q in queries), "empty_queries": sum(not q["relevant_photo_ids"] for q in queries),
          "multi_positive_queries": sum(len(q["relevant_photo_ids"]) > 1 for q in queries),
          "tag_counts": {tag: sum(tag in q["tags"] for q in queries) for tag in tags}, "frozen_at": now,
          "retrieval_outputs_seen_before_freeze": False, "paid_model_annotation_calls": 0,
          "sampling_plan_sha256": sha(OUT / "sampling-plan.json"), "limitations": annotations["limitations"]})
    attribution = ["# Validation image attribution", "", "40 real photographs from Wikimedia Commons; retain each listed attribution and license when redistributing these thumbnails. These files are only evaluation inputs; captions and labels must not enter the system under test.", ""]
    for p in provenance:
        attribution += [f"- **{p['photo_id']}** — [{p['commons_title']}]({p['source_page']}), {p['author']}. License: [{p['license']}]({p['license_url'] or p['source_page']})."]
    (OUT / "ATTRIBUTION.md").write_text("\n".join(attribution) + "\n", encoding="utf-8")
    artifacts = [OUT / n for n in ["sampling-plan.json", "annotations.json", "corpus.json", "queries.jsonl", "qrels.tsv", "meta.json", "review.json", "provenance.json", "leakage-audit.json", "ATTRIBUTION.md"]]
    artifacts += [ROOT / p["path"] for p in corpus]
    artifacts += [ROOT / p["source_api_path"] for p in provenance]
    artifacts += sorted(OUT.glob("supplement-plan*.json"))
    artifacts += [OUT / "acquisition.jsonl"]
    write("freeze.json", {"version": VERSION, "frozen_at": now, "retrieval_outputs_observed": False,
          "files": {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(artifacts))}})
    print(json.dumps({"photos": len(corpus), "queries": len(queries), "exact_duplicates": len(exact), "phash_cross_alerts": len(near), "phash_within_alerts": len(within)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
