"""Isolated September regression run; preserves all historical evidence.

prepare is offline. probe/run require the saved explicit outbound approval.
Agent allocation: 3 CNY; retrieval allocation: 27 CNY, including preflight.
"""

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / ".project-to-act/tasks/MODEL-RETEST-20260912"
EVIDENCE = TASK / "evidence"
OLD = ROOT / ".project-to-act/tasks/S6-RETRIEVAL-20260908/evidence"


def bootstrap():
    # Redirect imported helpers before any module captures its evidence path.
    from scripts.retrieval_eval import environment

    environment.TASK, environment.EVIDENCE = TASK, EVIDENCE
    from scripts.retrieval_eval import run as runner, provider
    from app.config import settings

    assert settings.database_url.endswith(":55449/photo_agent_retrieval_eval_test")
    assert settings.redis_url == "redis://127.0.0.1:56399/15"
    assert not settings.search_visual_verify_enabled, "default configuration changed"
    assert settings.qwen_chat_model == "qwen-plus"
    assert settings.qwen_embedding_model == "text-embedding-v3"
    provider.PRICING["checked_date"] = "2026-09-12"
    provider.PRICING["note"] += "; vision disabled for this retest"
    settings.search_cache_revision = "model-retest-20260912"
    runner.VARIANTS["CURRENT"] = {
        "verify_constraints": True,
        "verify_semantic": True,
        "visual": settings.search_visual_verify_enabled,
    }
    runner.Ledger = lambda: provider.Ledger(cap_micro=27_000_000)
    return runner, provider


def verify_authorization():
    value = json.loads((EVIDENCE / "outbound-approval.json").read_text())
    assert value["status"] == "authorized" and value["total_cap_cny"] == 30


async def prepare():
    runner, _ = bootstrap()
    if runner.FREEZE.exists():
        raise RuntimeError("Existing freeze: do not overwrite or reseed")
    from app.database import engine, AsyncSessionLocal, Base
    from app.models import Photo, User
    from sqlalchemy import text
    from app.config import settings

    for name in ("development-index-snapshot.json", "validation-index-snapshot.json"):
        dest = EVIDENCE / name
        if not dest.exists():
            shutil.copyfile(OLD / name, dest)
    corpus, queries = runner.load_dataset("validation")
    records = json.loads(
        (EVIDENCE / "validation-index-snapshot.json").read_text(encoding="utf8")
    )
    assert len(corpus) == len(records) == 40 and len(queries) == 80
    assert {r["id"] for r in records} == {p["database_photo_id"] for p in corpus}
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalar(text("SELECT count(*) FROM users")) == 0
        ), "not an empty isolated database"
        db.add(
            User(
                id=UUID(runner.VALIDATION_USER),
                wechat_openid="model-retest-validation",
                nickname="Evaluation fixture",
            )
        )
        await db.flush()
        for raw in records:
            row = {
                col.name: raw[col.name]
                for col in Photo.__table__.columns
                if col.name in raw
            }
            for col in Photo.__table__.columns:
                if row.get(col.name) is None:
                    continue
                if str(col.type) == "UUID":
                    row[col.name] = UUID(row[col.name])
                elif "DATETIME" in str(col.type).upper():
                    row[col.name] = datetime.fromisoformat(row[col.name])
            if isinstance(row.get("embedding"), str):
                row["embedding"] = json.loads(row["embedding"])
            db.add(Photo(**row))
        await db.commit()
    await runner.verify_database(
        "validation", runner.VALIDATION_USER, AsyncSessionLocal
    )
    runner.freeze()
    frozen = json.loads(runner.FREEZE.read_text(encoding="utf8"))
    extras = [
        ROOT / "scripts/retest_current_models.py",
        ROOT / "scripts/eval_loop_migration.py",
        ROOT / "tests/eval/agent/comfort_validation_v1.jsonl",
        EVIDENCE / "validation-index-snapshot.json",
    ]
    extras += [
        p for p in (ROOT / "tests/eval/retrieval_validation").rglob("*") if p.is_file()
    ]
    for path in extras:
        frozen["sources"][path.relative_to(ROOT).as_posix()] = runner.digest(path)
    frozen["version"] = "current-regression-20260912"
    frozen["agent_settings"] = {
        key: value
        for key, value in settings.model_dump().items()
        if key.startswith("agent_")
    }
    frozen["limitations"] = [
        "Previously seen labels and index; regression not blind test",
        "Query parsing disabled and scoring clock fixed as in the original retrieval protocol",
        "Current visual verification is disabled; index ingestion not rerun",
    ]
    runner.save(runner.FREEZE, frozen)
    runner.save(
        EVIDENCE / "validation-freeze.json",
        {
            "selected_variant": "CURRENT",
            "sources": frozen["sources"],
            "selection_basis": "Current configured behavior, no outcome-based selection",
        },
    )
    # Preserve actual input bytes as well as hashes; no environment/secrets files.
    import zipfile

    with zipfile.ZipFile(
        EVIDENCE / "frozen-inputs.zip", "x", zipfile.ZIP_DEFLATED
    ) as archive:
        for name in frozen["sources"]:
            archive.write(ROOT / name, name)
    await engine.dispose()
    print(
        json.dumps(
            {
                "prepared_photos": 40,
                "queries": 80,
                "freeze_sha256": runner.digest(runner.FREEZE),
            }
        )
    )


async def probe():
    runner, provider = bootstrap()
    verify_authorization()
    runner.verify_freeze()
    dest = EVIDENCE / "probe.json"
    if dest.exists():
        raise RuntimeError("Probe already attempted")
    runner.save(dest, {"status": "started"})
    from app.services.ai import embed_query

    ledger = runner.Ledger()
    try:
        with provider.call_context(
            "preflight", "embedding"
        ), provider.metered_transport(ledger):
            vector = await embed_query("模型复测连通性检查")
        assert len(vector) == 1024
        result = {
            "status": "passed",
            "dimension": len(vector),
            "ledger": ledger.summary(),
        }
    except Exception as exc:
        result = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "ledger": ledger.summary(),
        }
    runner.save(dest, result)
    print(json.dumps(result))


async def execute():
    runner, _ = bootstrap()
    verify_authorization()
    assert json.loads((EVIDENCE / "probe.json").read_text())["status"] == "passed"
    await runner.run("validation", "CURRENT")


def report():
    # Pure offline scoring, no bootstrap/provider access.
    from scripts.retrieval_eval.metrics import score_results

    directory = ROOT / "tests/eval/retrieval_validation"
    queries = [
        json.loads(line)
        for line in (directory / "queries.jsonl")
        .read_text(encoding="utf8")
        .splitlines()
        if line.strip()
    ]
    corpus = json.loads((directory / "corpus.json").read_text(encoding="utf8"))
    rows = [
        json.loads(
            (EVIDENCE / "runs/validation/CURRENT" / f"{q['id']}.json").read_text(
                encoding="utf8"
            )
        )
        for q in queries
    ]
    assert all(
        row.get("attempted") for row in rows
    ), "Incomplete paid run; do not score as complete"
    scored = score_results(queries, rows, [p["photo_id"] for p in corpus])
    (EVIDENCE / "retrieval-summary.json").write_text(
        json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(json.dumps(scored["aggregate"], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["prepare", "probe", "run", "report", "verify"]
    )
    action = parser.parse_args().action
    if action == "report":
        report()
    elif action == "verify":
        runner, _ = bootstrap()
        print(
            json.dumps(
                {
                    "verified": runner.verify_freeze()["version"],
                    "sha256": hashlib.sha256(runner.FREEZE.read_bytes()).hexdigest(),
                }
            )
        )
    else:
        asyncio.run({"prepare": prepare, "probe": probe, "run": execute}[action]())
