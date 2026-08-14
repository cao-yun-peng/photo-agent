"""Collect real retrieval rankings by invoking the current endpoint handler directly.

This is an evaluation-only fallback for cases where the FastAPI application shell
cannot become ready. It still executes query parsing, DashScope embeddings,
PostgreSQL/pgvector retrieval, and production hybrid ranking.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID


def _load_project(project_root: Path) -> tuple[Any, Any, Any, Any, Any]:
    sys.path.insert(0, str(project_root.resolve()))
    from sqlalchemy import select

    from app.api.search import semantic_search
    from app.database import AsyncSessionLocal
    from app.models.user import User
    from app.schemas.photo import SearchQuery
    from app.services import search as search_service

    return semantic_search, AsyncSessionLocal, User, (SearchQuery, select), search_service


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    semantic_search, session_factory, user_model, helpers, search_service = (
        _load_project(args.project_root)
    )
    search_query, select = helpers
    query_payload = json.loads(args.queries.read_text(encoding="utf-8"))
    queries = query_payload["queries"]
    import_map = json.loads(args.import_map.read_text(encoding="utf-8"))
    user_id = UUID(import_map["user"]["id"])
    uuid_to_dataset = {
        record["photo_id"]: record["dataset_id"] for record in import_map["records"]
    }

    results: dict[str, list[str]] = {}
    diagnostics: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    started = time.perf_counter()

    try:
        async with session_factory() as db:
            user = (
                await db.execute(select(user_model).where(user_model.id == user_id))
            ).scalar_one()
            for index, query in enumerate(queries, 1):
                query_started = time.perf_counter()
                try:
                    response = await semantic_search(
                    payload=search_query(
                        q=query["query"],
                        limit=args.limit,
                        auto_parse=args.auto_parse,
                    ),
                    current_user=user,
                    db=db,
                )
                    returned_ids: list[str] = []
                    item_details: list[dict[str, Any]] = []
                    for item in response.items:
                        photo_uuid = str(item.id)
                        dataset_id = uuid_to_dataset.get(photo_uuid)
                        if dataset_id is None:
                            raise ValueError(f"unmapped photo UUID: {photo_uuid}")
                        returned_ids.append(dataset_id)
                        item_details.append(
                            {
                                "photo_id": dataset_id,
                                "photo_uuid": photo_uuid,
                                "score_semantic": item.score_semantic,
                                "score_recency": item.score_recency,
                                "score_interaction": item.score_interaction,
                                "score_final": item.score_final,
                                "ai_description": item.ai_description,
                            }
                        )
                    results[query["id"]] = returned_ids
                    diagnostics[query["id"]] = {
                        "elapsed_seconds": round(
                            time.perf_counter() - query_started, 4
                        ),
                        "cache_hit": response.cache_hit,
                        "parsed": response.parsed.model_dump(mode="json")
                        if response.parsed
                        else None,
                        "items": item_details,
                    }
                    print(
                        f"[{index:02d}/{len(queries)}] {query['id']} "
                        f"{time.perf_counter() - query_started:.2f}s -> {returned_ids}",
                        flush=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "query_id": query["id"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    print(
                        f"[{index:02d}/{len(queries)}] {query['id']} ERROR "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
    finally:
        if search_service._redis is not None:
            await search_service._redis.aclose()
            search_service._redis = None

    return {
        "protocol": {
            "mode": "direct_endpoint_handler",
            "auto_parse": args.auto_parse,
            "limit": args.limit,
            "query_count": len(queries),
            "no_retry": True,
            "project_root": str(args.project_root.resolve()),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "results": results,
        "diagnostics": diagnostics,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect direct real retrieval rankings")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--import-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--auto-parse", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args()
    output = asyncio.run(collect(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"completed={len(output['results'])} errors={len(output['errors'])} "
        f"elapsed={output['elapsed_seconds']:.2f}s output={args.output}",
        flush=True,
    )
    return 0 if not output["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
