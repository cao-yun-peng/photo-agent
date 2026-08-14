"""调用正在运行的 Photo Agent 搜索接口，导出 retrieval_eval 所需排名结果。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx


def _stable_id(item: dict[str, Any], uuid_map: dict[str, str]) -> str | None:
    for key in ("source_id", "dataset_id", "photo_id"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("p-"):
            return value
    item_id = str(item.get("id", ""))
    return uuid_map.get(item_id)


def _load_uuid_map(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "records" in payload:
        return {
            str(record["photo_id"]): str(record["dataset_id"])
            for record in payload["records"]
        }
    return {str(photo_uuid): str(dataset_id) for photo_uuid, dataset_id in payload.items()}


async def collect(
    base_url: str,
    token: str,
    queries_path: str,
    *,
    split: str | None,
    limit: int,
    uuid_map_path: str | None,
    trust_env: bool,
) -> dict[str, Any]:
    payload = json.loads(Path(queries_path).read_text(encoding="utf-8"))
    queries = [
        query
        for query in payload["queries"]
        if split is None or query.get("split") == split
    ]
    uuid_map = _load_uuid_map(uuid_map_path)
    headers = {"Authorization": f"Bearer {token}"}
    results, diagnostics, errors = {}, {}, []
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=60,
        trust_env=trust_env,
    ) as client:
        for query in queries:
            try:
                response = await client.post(
                    "/search",
                    json={"q": query["query"], "limit": limit, "auto_parse": True},
                )
                response.raise_for_status()
                body = response.json()
                ids = [_stable_id(item, uuid_map) for item in body.get("items", [])]
                unresolved = [
                    item.get("id")
                    for item, photo_id in zip(body.get("items", []), ids, strict=True)
                    if not photo_id
                ]
                if unresolved:
                    raise ValueError(
                        "搜索响应没有稳定 p-xxx ID；请提供 --uuid-map，未解析 UUID="
                        + ",".join(map(str, unresolved))
                    )
                results[query["id"]] = ids
                diagnostics[query["id"]] = {
                    "parsed": body.get("parsed"),
                    "cache_hit": body.get("cache_hit"),
                    "items": [
                        {
                            "photo_id": photo_id,
                            "photo_uuid": str(item.get("id", "")),
                            "score_semantic": item.get("score_semantic"),
                            "score_recency": item.get("score_recency"),
                            "score_interaction": item.get("score_interaction"),
                            "score_final": item.get("score_final"),
                            "ai_description": item.get("ai_description"),
                        }
                        for item, photo_id in zip(
                            body.get("items", []), ids, strict=True
                        )
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"query_id": query["id"], "error": f"{type(exc).__name__}: {exc}"}
                )
    return {
        "protocol": {
            "mode": "http_api",
            "base_url": base_url.rstrip("/"),
            "auto_parse": True,
            "limit": limit,
            "split": split,
            "trust_env": trust_env,
            "no_retry": True,
        },
        "results": results,
        "diagnostics": diagnostics,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="采集真实图片检索排名")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token",
        default=os.getenv("PHOTO_EVAL_JWT"),
        help="测试用户 JWT；默认读取 PHOTO_EVAL_JWT，不要写入文件或提交 Git",
    )
    parser.add_argument("--queries", default="tests/eval/retrieval_queries.json")
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--trust-env",
        action="store_true",
        help="继承 HTTP(S)_PROXY 等环境变量；本地 127.0.0.1 评测默认关闭",
    )
    parser.add_argument("--uuid-map", help="数据库 UUID 到 p-xxx 的 JSON 映射")
    parser.add_argument("--output", default="artifacts/retrieval-results.json")
    args = parser.parse_args()
    if not args.token:
        parser.error("请通过 PHOTO_EVAL_JWT 或 --token 提供测试用户 JWT")
    output = asyncio.run(
        collect(
            args.base_url,
            args.token,
            args.queries,
            split=args.split,
            limit=args.limit,
            uuid_map_path=args.uuid_map,
            trust_env=args.trust_env,
        )
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"已采集 {len(output['results'])} 条；错误 {len(output['errors'])} 条；输出 {path}"
    )
    return 0 if not output["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
