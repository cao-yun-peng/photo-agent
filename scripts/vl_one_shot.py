"""单图调试：调用真实 VL 并打印结构化 JSON。

用法示例：
    python scripts/vl_one_shot.py --image test_photos/p-004_ramen.jpg
    python scripts/vl_one_shot.py --image https://example.com/p-004_ramen.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.services import ai as ai_service  # noqa: E402


def _to_image_input(image: str) -> str:
    """把本地路径转换为 data URL；HTTP(S)/data URL 原样返回。"""
    raw = image.strip()
    lower = raw.lower()
    if lower.startswith("http://") or lower.startswith("https://") or lower.startswith("data:"):
        return raw

    path = Path(raw)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"图片不存在: {path}")

    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _is_mock_mode() -> bool:
    value = (settings.dashscope_api_key or "").strip()
    return value in ("", "sk-xxx", "please_set_dashscope_key")


async def _run(image: str) -> int:
    image_input = _to_image_input(image)
    result = await ai_service.analyze_image(image_input)
    print(json.dumps(result.model_dump(exclude_none=True), ensure_ascii=False, indent=2))
    if _is_mock_mode():
        print("\n[warn] 当前是 MOCK 模式，请在 .env 设置真实 DASHSCOPE_API_KEY 以调用真实模型。")
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="单图调用 Qwen-VL 并输出结构化 JSON")
    parser.add_argument(
        "--image",
        required=True,
        help="图片输入：本地路径、HTTP(S) URL 或 data URL",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_run(args.image))
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
