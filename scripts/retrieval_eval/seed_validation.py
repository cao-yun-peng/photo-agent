"""Isolated validation ingestion using the application's genuine image pipeline.

Interface (from the repository root):
  python -m scripts.retrieval_eval.seed_validation --check
  python -m scripts.retrieval_eval.seed_validation
  python -m scripts.retrieval_eval.seed_validation --retry-failed

--check is offline. Other modes fail closed unless outbound-approval.json says
authorized. Successful stages are reused, including the app's completed analysis
fallback. Only failed stages are eligible for --retry-failed. An interrupted or
ambiguous attempt additionally needs --acknowledge-ambiguous REASON; every old
attempt and conservative provider charge remains intact. Never run ingestion
after validation-freeze.json exists. No scheduler or source database is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx

from scripts.retrieval_eval.environment import EVIDENCE, ROOT, VALIDATION_USER, configure
from scripts.retrieval_eval.provider import Ledger, MoneyCapExceeded, call_context, metered_transport

configure()
DATASET = ROOT / "tests/eval/retrieval_validation"
VARIANT = "validation-ingestion"


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        # Preserve bad numeric evidence in valid JSON; numpy's quality gate still
        # recognizes these strings as non-finite and prevents vector insertion.
        return str(value)
    if isinstance(value, dict):
        return {str(k): safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        temporary = Path(handle.name)
        json.dump(safe_value(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def require_approval(evidence=EVIDENCE):
    try:
        approval = json.loads((evidence / "outbound-approval.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("explicit_data_egress_approval_pending") from exc
    if approval.get("status") != "authorized":
        raise RuntimeError("explicit_data_egress_approval_pending")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def bind_frozen_configuration():
    from scripts.retrieval_eval.run import verify_freeze
    frozen = verify_freeze()
    from app.config import settings
    for key, value in frozen["settings"].items():
        setattr(settings, key, value)
    return frozen


def load_corpus(dataset=DATASET, root=ROOT):
    frozen = json.loads((dataset / "freeze.json").read_text(encoding="utf-8"))
    for name, expected in frozen["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("validation_frozen_input_changed")
    corpus = json.loads((dataset / "corpus.json").read_text(encoding="utf-8"))
    if len(corpus) != 40 or len({p["database_photo_id"] for p in corpus}) != 40:
        raise RuntimeError("validation_corpus_identity_error")
    # Explicit allowlist ensures labels, observations, titles and metadata cannot
    # accidentally be passed into the production image/model calls.
    return [{key: p[key] for key in ("photo_id", "database_photo_id", "path", "sha256", "width", "height")} for p in corpus]


class StageUnavailable(RuntimeError):
    pass


class StageJournal:
    def __init__(self, ledger, evidence=EVIDENCE, retry_failed=False, acknowledge_ambiguous=""):
        self.ledger, self.evidence = ledger, evidence
        self.directory = evidence / "validation-ingestion"
        self.retry_failed = retry_failed
        self.acknowledgment = acknowledge_ambiguous.strip()

    def calls(self, query_id):
        with self.ledger.connect() as db:
            return dict(db.execute("SELECT id,status FROM calls WHERE variant=? AND query_id=?", (VARIANT, query_id)).fetchall())

    async def execute(self, photo_id, stage, identity, operation, serialize=lambda x: x):
        require_approval(self.evidence)
        folder = self.directory / photo_id
        key = fingerprint(identity)
        result_path = folder / f"{stage}-result.json"
        if result_path.exists():
            existing = json.loads(result_path.read_text(encoding="utf-8"))
            if existing["input_sha256"] != key:
                raise RuntimeError("completed_stage_input_changed")
            return existing["value"]
        attempts = sorted(folder.glob(f"{stage}-attempt-*.json"))
        previous = json.loads(attempts[-1].read_text(encoding="utf-8")) if attempts else None
        if previous:
            if previous["input_sha256"] != key:
                raise RuntimeError("failed_stage_input_changed")
            ambiguous = previous["status"] in {"started", "ambiguous"}
            if ambiguous and not self.acknowledgment:
                raise StageUnavailable("ambiguous_attempt_requires_explicit_acknowledgment")
            if not ambiguous and not self.retry_failed:
                raise StageUnavailable("failed_stage_requires_retry_failed")
        query_id = f"{photo_id}:{stage}"
        before = self.calls(query_id)
        attempt_path = folder / f"{stage}-attempt-{len(attempts) + 1:04d}.json"
        attempt = {"photo_id": photo_id, "stage": stage, "input_sha256": key, "started_at": now(), "status": "started",
                   "previous_attempt": attempts[-1].name if attempts else None,
                   "ambiguity_acknowledgment": self.acknowledgment if previous and previous["status"] in {"started", "ambiguous"} else None}
        save(attempt_path, attempt)
        try:
            with call_context(VARIANT, query_id), metered_transport(self.ledger):
                value = serialize(await operation())
            calls = {k: v for k, v in self.calls(query_id).items() if k not in before}
            status = "completed_partial" if isinstance(value, dict) and value.get("parse_quality", "ok") != "ok" else "succeeded"
            # An analysis fallback is the application's completed output and is
            # reused exactly as the worker reuses its matching analysis_version.
            save(result_path, {"input_sha256": key, "status": status, "value": value, "completed_at": now(), "provider_calls": calls})
            attempt.update(status=status, completed_at=now(), provider_calls=calls)
            save(attempt_path, attempt)
            return value
        except BaseException as exc:
            calls = {k: v for k, v in self.calls(query_id).items() if k not in before}
            ambiguous = any(v in {"reserved", "ambiguous_failure"} for v in calls.values()) or isinstance(exc, (httpx.RequestError, asyncio.CancelledError))
            attempt.update(status="ambiguous" if ambiguous else "failed", completed_at=now(), provider_calls=calls, error_code=type(exc).__name__)
            save(attempt_path, attempt)
            raise


async def analyze_photo(record, journal, ai_service=None, root=ROOT, image_loader=None):
    from app.schemas.analysis import ImageAnalysis
    from app.services import ai, image as image_service
    from app.services.circuit_breaker import ServiceDegradedError
    from app.services.quality import preflight_check, quality_gate, decide_storage
    from app.services.semantic_facets import apply_semantic_facets, clear_semantic_facets
    from scripts.retrieval_eval.run import image_input
    from types import SimpleNamespace

    service = ai_service or ai
    path = (root / record["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RuntimeError("validation_image_outside_workspace")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise RuntimeError("validation_image_changed")
    preflight = preflight_check(raw)
    metadata = {"width": preflight.width, "height": preflight.height, "size_bytes": len(raw), "mime_type": "image/jpeg"}
    folder = journal.directory / record["photo_id"]
    save(folder / "preflight.json", asdict(preflight))
    if not preflight.ok:
        return {**metadata, "status": "skipped", "partial_reason": preflight.reason, "ai_description": None, "ai_analysis": {}, "embedding": None}
    processed = image_service.process(raw, thumb_max=512)
    thumb_path = folder / "thumbnail.jpg"
    thumb_path.write_bytes(processed.thumb_bytes)
    metadata.update(width=processed.width, height=processed.height, taken_at=processed.taken_at, location=processed.location,
                    thumb_key="eval-local:" + thumb_path.relative_to(root).as_posix())
    save(folder / "image-processing.json", {**metadata, "thumbnail_sha256": hashlib.sha256(processed.thumb_bytes).hexdigest()})
    # Only the original image bytes reach VL. Description/analysis are generated
    # by the app, never copied from provenance or evaluation annotations.
    image_url = (image_loader or image_input)("eval-local:" + record["path"])
    base = {"photo_sha256": record["sha256"], "vl_model": service.settings.qwen_vl_model}
    try:
        description = await journal.execute(record["photo_id"], "describe", {**base, "prompt": fingerprint(service._VL_PROMPT)}, lambda: service.describe_image(image_url))
    except MoneyCapExceeded:
        raise
    except Exception as exc:
        save(folder / "outcome.json", {"status": "partial_done" if isinstance(exc, ServiceDegradedError) else "failed", "error_code": type(exc).__name__})
        return {**metadata, "status": "partial_done" if isinstance(exc, ServiceDegradedError) else "failed", "partial_reason": "vl_degraded" if isinstance(exc, ServiceDegradedError) else "vl_failure"}
    try:
        payload = await journal.execute(record["photo_id"], "analyze", {**base, "prompt": fingerprint(service._VL_ANALYSIS_PROMPT), "version": service.VL_ANALYSIS_PROMPT_VERSION},
                                        lambda: service.analyze_image(image_url), lambda x: x.model_dump(exclude_none=True))
        analysis = ImageAnalysis.model_validate(payload)
    except MoneyCapExceeded:
        raise
    except Exception as exc:
        save(folder / "outcome.json", {"status": "failed", "error_code": type(exc).__name__, "description_preserved": True})
        return {**metadata, "status": "failed", "partial_reason": "analysis_failure", "ai_description": description}
    text_input = service.build_retrieval_text(description, analysis)
    save(folder / "retrieval-text.json", {"text": text_input, "sha256": fingerprint(text_input), "origin": "application_model_outputs_only"})
    partial_reason = None
    embedding_error = None
    try:
        embedding = await journal.execute(record["photo_id"], "embedding", {"text_sha256": fingerprint(text_input), "model": service.settings.qwen_embedding_model, "text_type": "document"}, lambda: service.embed_text(text_input))
    except MoneyCapExceeded:
        # Retain successful VL products before the batch stops scheduling calls.
        embedding, partial_reason, embedding_error = None, "embedding_service_busy", "MoneyCapExceeded"
    except Exception as exc:
        embedding = None
        partial_reason = "embedding_service_busy" if isinstance(exc, ServiceDegradedError) else "embedding_retrying"
        embedding_error = type(exc).__name__
    gate = quality_gate(description=description, embedding=embedding, analysis=analysis)
    decision = decide_storage(gate)
    facets = SimpleNamespace()
    if decision.store_analysis:
        apply_semantic_facets(facets, analysis)
    else:
        clear_semantic_facets(facets)
    fields = {**metadata, "ai_description": description if decision.store_description else None,
              "ai_analysis": analysis.model_dump(exclude_none=True) if decision.store_analysis else {},
              "embedding": embedding if decision.store_embedding else None,
              "status": decision.status, "partial_reason": partial_reason or decision.partial_reason,
              "photo_type": facets.photo_type, "is_selfie": facets.is_selfie, "people_count": facets.people_count,
              "embedding_last_error": embedding_error, "embedding_next_retry_at": None}
    save(folder / "outcome.json", {"status": fields["status"], "partial_reason": fields["partial_reason"], "quality_gate": asdict(gate), "storage_decision": asdict(decision),
                                 "index_ready": fields["embedding"] is not None and fields["status"] in {"done", "partial_done"}})
    return fields


async def main(args):
    corpus = load_corpus()
    if args.check:
        print(json.dumps({"status": "offline_inputs_valid", "photos": len(corpus), "model_calls": 0}))
        return
    require_approval()
    if (EVIDENCE / "validation-freeze.json").exists():
        raise RuntimeError("validation_index_already_frozen")
    bind_frozen_configuration()
    from app.config import settings
    from app.database import engine, AsyncSessionLocal, Base
    from app.models import Photo, User
    from app.services import ai
    from sqlalchemy import text, select
    from sqlalchemy.engine import make_url
    configured = make_url(settings.database_url)
    if configured.host != "127.0.0.1" or configured.port != 55449 or configured.database != "photo_agent_retrieval_eval_test":
        raise RuntimeError("database_is_not_isolated_evaluation")
    if ai.is_mock():
        raise RuntimeError("real_validation_ingestion_rejects_mock_provider")
    # App exception logging can contain provider response bodies; stage evidence
    # and the existing metered response store are the auditable record instead.
    logging.getLogger("app.services.ai").setLevel(logging.CRITICAL)
    ledger = Ledger()
    journal = StageJournal(ledger, retry_failed=args.retry_failed, acknowledge_ambiguous=args.acknowledge_ambiguous)
    async with engine.connect() as lock:
        held = await lock.scalar(text("SELECT pg_try_advisory_lock(hashtext(:scope))"), {"scope": VARIANT + VALIDATION_USER})
        if not held:
            raise RuntimeError("validation_ingestion_already_running")
        await lock.commit()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.create_all)
            async with AsyncSessionLocal() as db:
                if not await db.get(User, UUID(VALIDATION_USER)):
                    db.add(User(id=UUID(VALIDATION_USER), wechat_openid="retrieval-eval-validation-20260908", nickname="Isolated real-photo validation"))
                    await db.flush()
                existing = set((await db.scalars(select(Photo.id).where(Photo.user_id == UUID(VALIDATION_USER)))).all())
                expected = {UUID(p["database_photo_id"]) for p in corpus}
                if not existing <= expected:
                    raise RuntimeError("unexpected_validation_user_photos")
                for p in corpus:
                    row = await db.get(Photo, UUID(p["database_photo_id"]))
                    if row:
                        if row.user_id != UUID(VALIDATION_USER) or row.hash != p["sha256"] or row.oss_key != "eval-local:" + p["path"]:
                            raise RuntimeError("validation_photo_identity_mismatch")
                    else:
                        db.add(Photo(id=UUID(p["database_photo_id"]), user_id=UUID(VALIDATION_USER), hash=p["sha256"], oss_key="eval-local:" + p["path"],
                                     thumb_key="eval-local:" + p["path"], width=p["width"], height=p["height"], status="pending"))
                await db.commit()  # All 40 survive even if indexing fails or the budget is exhausted.
                for p in corpus:
                    if ledger.exhausted:
                        break
                    row = await db.get(Photo, UUID(p["database_photo_id"]))
                    try:
                        fields = await analyze_photo(p, journal)
                    except MoneyCapExceeded:
                        row.status, row.partial_reason = "partial_done", "evaluation_budget_exhausted"
                        await db.commit()
                        break
                    for key, value in fields.items():
                        setattr(row, key, value)
                    await db.commit()
                    print(json.dumps({"photo_id": p["photo_id"], "status": row.status, "index_ready": row.search_index_status == "ready"}), flush=True)
                # Full SQL row_to_json shape, identical to development snapshots.
                records = await db.scalar(text("SELECT coalesce(json_agg(row_to_json(p)), '[]'::json) FROM photos p WHERE user_id=CAST(:u AS uuid)"), {"u": VALIDATION_USER})
                if len(records) != len(corpus):
                    raise RuntimeError("validation_snapshot_lost_photos")
                save(EVIDENCE / "validation-index-snapshot.json", records)
                summary = {"completed_at": now(), "photos": len(records), "statuses": dict(Counter(r["status"] for r in records)),
                           "index_ready": sum(r["embedding"] is not None and r["status"] in {"done", "partial_done"} for r in records),
                           "usage": ledger.summary(variant=VARIANT), "source_account_access": "none", "automatic_retry_scheduler": False}
                save(EVIDENCE / "validation-ingestion-summary.json", summary)
                print(json.dumps(summary))
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(hashtext(:scope))"), {"scope": VARIANT + VALIDATION_USER})
            await lock.commit()
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Verify frozen local inputs only; no DB or provider access")
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly retry failed stages, preserving all successful outputs and attempts")
    parser.add_argument("--acknowledge-ambiguous", default="", metavar="REASON", help="Recorded operator reason for retrying a billed/unknown prior attempt")
    asyncio.run(main(parser.parse_args()))
