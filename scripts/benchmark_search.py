"""Reproducible synthetic PG/Redis benchmark; refuses non-test databases."""

import asyncio
import gc
import json
import os
from pathlib import Path
import statistics
import sys
import time
import tracemalloc
from types import SimpleNamespace
from uuid import uuid4
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import conftest  # noqa: F401,E402
import numpy as np  # noqa: E402
from sqlalchemy import select, text, delete, insert  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.photo import Photo  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.search_contracts import SearchRequest  # noqa: E402
from app.services.search_repository import SearchRepository  # noqa: E402
from app.services.search import recency_score, semantic_score, combine  # noqa: E402


async def measure(fn, repeats=5):
    await fn()  # warm-up, separate from measured first invocation
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = await fn()
        times.append((time.perf_counter() - start) * 1000)
    gc.collect()
    tracemalloc.start()
    await fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, {
        "median_ms": round(statistics.median(times), 3),
        "p95_ms": round(max(times), 3),
        "python_peak_bytes": peak,
        "samples": times,
    }


async def main():
    url = make_url(settings.database_url)
    assert (
        os.getenv("PHOTO_AGENT_TEST_DATABASE_URL")
        and url.host in {"127.0.0.1", "localhost"}
        and url.database == "photo_agent_batch1_test"
    )
    engine = create_async_engine(
        settings.database_url, connect_args={"prepared_statement_cache_size": 0}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users = [uuid4() for _ in range(4)]
    rng = np.random.default_rng(4092026)
    count = 6000
    vectors = rng.normal(size=(count, 1024)).astype("float32")
    vectors /= np.linalg.norm(vectors, axis=1)[:, None]
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    owners = [users[0]] * 3000 + [users[1]] * 300 + [users[2]] * 30 + [users[3]] * 2670
    report = {
        "seed": 4092026,
        "rows": count,
        "dimensions": 1024,
        "tenants": [3000, 300, 30, 2670],
        "warm_cache": True,
        "clock": now.isoformat(),
        "ann": [],
    }
    try:
        async with factory() as db:
            db.add_all(User(id=u, wechat_openid="benchmark-" + str(u)) for u in users)
            await db.commit()
            for start in range(0, count, 100):
                await db.execute(
                    insert(Photo),
                    [
                        {
                            "id": uuid4(),
                            "user_id": owners[i],
                            "hash": uuid4().hex,
                            "oss_key": "synthetic/benchmark.jpg",
                            "status": "done",
                            "created_at": now - timedelta(days=365),
                            "updated_at": now - timedelta(days=365),
                            "embedding": vectors[i].tolist(),
                            "taken_at": now - timedelta(days=i % 180),
                            "ai_analysis": {"objects": ["synthetic"]},
                        }
                        for i in range(start, min(start + 100, count))
                    ],
                )
            await db.commit()
            await db.execute(text("ANALYZE photos"))
            report["postgres"] = (await db.execute(text("SELECT version()"))).scalar()
            report["pgvector"] = (
                await db.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            ).scalar()
            await db.commit()
        query = vectors[14].tolist()
        plan = SimpleNamespace(timezone="UTC", scoring_time=now.isoformat())
        req = SearchRequest(
            q="synthetic",
            retrieval_mode="album",
            result_mode="select",
            w_semantic=0.4,
            w_recency=0.35,
            w_interaction=0.25,
        )

        async def old(full=False):
            async with factory() as db:
                dist = Photo.embedding.cosine_distance(query)
                stmt = (
                    select(Photo, dist)
                    .where(Photo.user_id == users[0])
                    .order_by(dist.asc(), Photo.id.asc())
                )
                if not full:
                    stmt = stmt.limit(settings.search_snapshot_max_candidates + 1)
                rows = (await db.execute(stmt)).all()
                scored = []
                for photo, distance in rows[
                    : len(rows) if full else settings.search_snapshot_max_candidates
                ]:
                    sem = semantic_score(distance)
                    rec = recency_score(photo.taken_at, now=now)
                    scored.append(
                        (str(photo.id), combine(sem, rec, 0, 0.4, 0.35, 0.25))
                    )
                return sorted(scored, key=lambda x: (-x[1], x[0]))[:20]

        async def new():
            async with factory() as db:
                rows, *_ = await SearchRepository(db, users[0]).recall(
                    req, plan, query, None
                )
                return [(str(x[0].id), x[-1]) for x in rows[:20]]

        old_ids, report["batch3_shortlist"] = await measure(old)
        exact_ids, report["full_orm_reference"] = await measure(lambda: old(True))
        sql_ids, report["sql_album"] = await measure(new)
        from app.services.search_album import album_page
        from app.services.search_engine import SearchService

        async def keyset():
            async with factory() as db:
                frozen = SimpleNamespace(
                    timezone="UTC",
                    scoring_time=now.isoformat(),
                    id=uuid4(),
                    expires_at=time.time() + 600,
                    request=lambda: req,
                )
                result = await album_page(
                    SearchService(db, users[0]),
                    req.model_copy(update={"limit": 20}),
                    frozen,
                    0,
                    {"vector": query},
                )
                return [(x["id"], x["score_final"]) for x in result["items"]]

        keyset_ids, report["sql_album_keyset"] = await measure(keyset)
        report["keyset_matches_exact_ids"] = [x[0] for x in keyset_ids] == [
            x[0] for x in exact_ids
        ]
        report["sql_matches_exact_ids"] = [x[0] for x in sql_ids] == [
            x[0] for x in exact_ids
        ]
        report["batch3_overlap_exact_at20"] = (
            len(set(x[0] for x in old_ids) & set(x[0] for x in exact_ids)) / 20
        )
        # Force an exact distance expression; default/iterative paths can use HNSW.
        for tenant in range(3):
            for limit in [20, 100]:
                queries = [vectors[i].tolist() for i in [4, 81, 131, 516, 2201]]
                exact = []
                async with factory() as db:
                    for q in queries:
                        d = Photo.embedding.cosine_distance(q)
                        exact.append(
                            set(
                                (
                                    await db.execute(
                                        select(Photo.id)
                                        .where(Photo.user_id == users[tenant])
                                        .order_by((d + 0.0).asc(), Photo.id)
                                        .limit(limit)
                                    )
                                )
                                .scalars()
                                .all()
                            )
                        )
                for ef, iterative, forced in [
                    (40, "off", False),
                    (40, "off", True),
                    (100, "strict_order", True),
                    (200, "strict_order", True),
                ]:
                    times = []
                    recalls = []
                    plan_names = []
                    async with factory() as db:
                        await db.execute(
                            text(
                                "SELECT set_config('hnsw.ef_search',:ef,true),set_config('hnsw.iterative_scan',:it,true)"
                            ),
                            {"ef": str(ef), "it": iterative},
                        )
                        if forced:
                            await db.execute(text("SET LOCAL enable_sort=off"))
                            await db.execute(text("SET LOCAL enable_bitmapscan=off"))
                            await db.execute(text("SET LOCAL enable_seqscan=off"))
                        for index, q in enumerate(queries):
                            d = Photo.embedding.cosine_distance(q)
                            stmt = (
                                select(Photo.id)
                                .where(Photo.user_id == users[tenant])
                                .order_by(d.asc())
                                .limit(limit)
                            )
                            start = time.perf_counter()
                            got = set((await db.execute(stmt)).scalars().all())
                            times.append((time.perf_counter() - start) * 1000)
                            recalls.append(len(got & exact[index]) / len(exact[index]))
                            if index == 0:
                                compiled = str(
                                    stmt.compile(
                                        dialect=db.bind.dialect,
                                        compile_kwargs={"literal_binds": True},
                                    )
                                )
                                analyzed = (
                                    await db.execute(
                                        text(
                                            "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
                                            + compiled
                                        )
                                    )
                                ).scalar()
                                plan_names = analyzed
                    report["ann"].append(
                        {
                            "tenant_rows": [3000, 300, 30][tenant],
                            "k": limit,
                            "ef_search": ef,
                            "forced_hnsw": forced,
                            "iterative": iterative,
                            "mean_recall": sum(recalls) / len(recalls),
                            "min_recall": min(recalls),
                            "median_ms": statistics.median(times),
                            "p95_ms": max(times),
                            "explain": plan_names,
                        }
                    )
        from redis.asyncio import Redis
        from app.services import lock, search as search_module
        from app.services.search_budget import SearchBudget, model_call

        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        lock._redis_client = redis
        calls = 0

        async def fake_embed(_query):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.1)
            return query

        previous = search_module.embed_query
        search_module.embed_query = fake_embed
        try:
            cache_metrics = {}
            for mode in ["uncoalesced", "cold_singleflight", "warm_cache"]:
                before = calls
                budgets = [SearchBudget(redis, users[0], uuid4()) for _ in range(20)]
                await asyncio.gather(*(b.create() for b in budgets))

                async def call(b):
                    with b.activate():
                        if mode == "uncoalesced":
                            return await model_call(
                                "embedding", lambda: fake_embed("synthetic benchmark")
                            )
                        return await search_module.get_query_embedding(
                            "synthetic benchmark"
                        )

                started = time.perf_counter()
                await asyncio.gather(*(call(b) for b in budgets))
                elapsed = (time.perf_counter() - started) * 1000
                usages = await asyncio.gather(*(b.usage() for b in budgets))
                cache_metrics[mode] = {
                    "concurrent_requests": 20,
                    "provider_calls": calls - before,
                    "wall_ms": elapsed,
                    "reserved_model_calls": sum(x["calls"] for x in usages),
                    "budget_units": sum(x["units"] for x in usages),
                }
            report["model_stub_benchmark"] = cache_metrics
        finally:
            search_module.embed_query = previous
            lock._redis_client = None
            await redis.aclose()
        out = Path("docs/benchmarks/batch4-search.json")
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k != "ann"}, indent=2))
        print(
            json.dumps(
                [{k: v for k, v in x.items() if k != "explain"} for x in report["ann"]],
                indent=2,
            )
        )
    finally:
        async with factory() as db:
            await db.execute(delete(User).where(User.id.in_(users)))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
