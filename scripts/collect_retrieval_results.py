"""调用正在运行的 Photo Agent 搜索接口，导出 retrieval_eval 所需排名结果。"""

from __future__ import annotations

import argparse
import asyncio
import json
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


async def collect(
    base_url: str,
    token: str,
    queries_path: str,
    *,
    split: str | None,
    limit: int,
    uuid_map_path: str | None,
) -> dict[str, Any]:
    payload = json.loads(Path(queries_path).read_text(encoding="utf-8"))
    queries = [
        query
        for query in payload["queries"]
        if split is None or query.get("split") == split
    ]
    uuid_map = (
        json.loads(Path(uuid_map_path).read_text(encoding="utf-8"))
        if uuid_map_path
        else {}
    )
    headers = {"Authorization": f"Bearer {token}"}
    results, errors = {}, []
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), headers=headers, timeout=60
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
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {"query_id": query["id"], "error": f"{type(exc).__name__}: {exc}"}
                )
    return {"results": results, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="采集真实图片检索排名")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--token", required=True, help="测试用户 JWT；不要写入文件或提交 Git"
    )
    parser.add_argument("--queries", default="tests/eval/retrieval_queries.json")
    parser.add_argument("--split", choices=["development", "validation", "test"])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--uuid-map", help="数据库 UUID 到 p-xxx 的 JSON 映射")
    parser.add_argument("--output", default="artifacts/retrieval-results.json")
    args = parser.parse_args()
    output = asyncio.run(
        collect(
            args.base_url,
            args.token,
            args.queries,
            split=args.split,
            limit=args.limit,
            uuid_map_path=args.uuid_map,
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
