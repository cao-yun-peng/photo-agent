"""DashScope 调用：通义千问 VL 生描述、Embedding 生向量。

用 httpx 直接调 HTTP API 而不是官方 SDK，因为：
1. 官方 SDK 是同步阻塞的，会卡住 async worker；
2. HTTP 版更透明，出问题好排查。

当 .env 里 DASHSCOPE_API_KEY 还是 "sk-xxx" 占位符时会走 mock 分支，
让 D3–D4 联调不受影响；填了真 key 就自动切到真调用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from typing import Any

import httpx

from app.config import settings
from app.schemas.analysis import ImageAnalysis
from app.services.circuit_breaker import (
    ServiceDegradedError,
    embedding_breaker,
    vl_breaker,
)
from app.services.metrics import metrics


logger = logging.getLogger(__name__)


def _stable_mock_seed(text: str) -> int:
    """生成跨进程稳定的 mock 随机种子。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


# ---- API 常量 ---------------------------------------------------------
_VL_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
_EMB_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
)

# 让 worker 有个合理超时；VL 慢一些，Embedding 快
_VL_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_EMB_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# 生成描述的 Prompt。改这里就等于改产品口吻。
_VL_PROMPT = (
    "请用一段简洁自然的中文描述这张图片，覆盖：主要物体或人物、场景/地点、氛围或情绪、"
    "可能的时间线索（如白天/夜晚、季节）。控制在 60 字以内，不要使用列表或标题格式。"
)

# 结构化分析 Prompt：要求 VL 输出固定 JSON 结构。
_VL_ANALYSIS_PROMPT = """请仔细分析这张图片，并以 JSON 格式返回以下字段：
{
  "scene": "场景大类，如 室内/户外/餐厅/景区/街道/居家/办公室/车内/其他，必填",
  "scene_detail": "更具体的场景描述，可选",
  "persons": {
    "count": "图中人数，整数，无法判断填 0",
    "age_estimate": "年龄段估计，如 儿童/青年/中年/老年/多个年龄段，可选",
    "expression": "表情或动作描述，可选"
  },
  "objects": ["照片中显著物体标签列表，如 食物、宠物、建筑、交通工具"],
  "text_in_image": ["图中识别到的文字片段，没有则留空数组"],
  "mood": "氛围词，如 温馨/热闹/安静/正式/轻松/紧张，可选",
  "colors": ["主色调，如 蓝色、白色"],
  "summary": "用一句话综合描述图片内容，用于搜索语义匹配，60 字以内"
}

注意：
1. 只返回 JSON，不要添加任何解释、markdown 代码块标记或其他文字。
2. 如果图片为空、损坏、纯文字截图或无法识别，objects 和 text_in_image 可以为空，但 scene 和 summary 必须给出合理值。
3. 所有字段必须使用中文。"""

# 从 VL 返回文本中抠 JSON 的兜底正则
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)



# ---- 是否走 mock ------------------------------------------------------
def _is_mock() -> bool:
    return not settings.dashscope_api_key or settings.dashscope_api_key.strip() in (
        "",
        "sk-xxx",
        "please_set_dashscope_key",
    )


# ---- 对外接口 ---------------------------------------------------------
async def describe_image(image_url: str) -> str:
    """调 qwen-vl 生成中文描述。失败时抛出 RuntimeError，让 worker 感知。"""
    if _is_mock():
        return "[mock 描述] 一张图片。（真实描述需要在 .env 填 DASHSCOPE_API_KEY）"

    async def _do_call() -> str:
        payload: dict[str, Any] = {
            "model": settings.qwen_vl_model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": image_url},
                            {"text": _VL_PROMPT},
                        ],
                    }
                ]
            },
            "parameters": {
                "result_format": "message",
                "max_tokens": 200,
            },
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=_VL_TIMEOUT) as client:
            resp = await client.post(_VL_URL, json=payload, headers=headers)

        if resp.status_code != 200:
            raise RuntimeError(
                f"DashScope VL HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()

        # 期望结构：{"output": {"choices": [{"message": {"content": [{"text": "..."}]}}]}}
        try:
            choices = data["output"]["choices"]
            content = choices[0]["message"]["content"]
            # content 可能是 str 也可能是 [{"text": "..."}]
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict)]
                description = "".join(texts).strip()
            else:
                description = str(content).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"DashScope VL unexpected response: {data}"
            ) from exc

        if not description:
            raise RuntimeError("DashScope VL returned empty description")
        return description

    try:
        return await vl_breaker.call(_do_call)
    except ServiceDegradedError:
        raise
    except Exception as exc:
        logger.warning("describe_image failed | exc=%s", exc)
        raise


async def analyze_image(image_url: str) -> ImageAnalysis:
    """调 qwen-vl 输出结构化分析结果。失败时仍返回 fallback 对象，不阻塞流程。"""
    if _is_mock():
        return ImageAnalysis(
            scene="unknown",
            scene_detail="mock 模式未启用真实 VL 分析",
            summary="[mock 摘要] 一张图片（真实分析需填写 DASHSCOPE_API_KEY）。",
            parse_quality="fallback",
        )

    async def _do_call() -> ImageAnalysis:
        payload: dict[str, Any] = {
            "model": settings.qwen_vl_model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": image_url},
                            {"text": _VL_ANALYSIS_PROMPT},
                        ],
                    }
                ]
            },
            "parameters": {
                "result_format": "message",
                "max_tokens": 800,
            },
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=_VL_TIMEOUT) as client:
            resp = await client.post(_VL_URL, json=payload, headers=headers)

        if resp.status_code != 200:
            logger.warning(
                "analyze_image HTTP failed | status=%s body=%s",
                resp.status_code,
                resp.text[:300],
            )
            return _fallback_analysis(f"vl_http_{resp.status_code}")

        data = resp.json()
        try:
            choices = data["output"]["choices"]
            content = choices[0]["message"]["content"]
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict)]
                raw_text = "".join(texts).strip()
            else:
                raw_text = str(content).strip()
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("analyze_image unexpected response | exc=%s data=%s", exc, data)
            return _fallback_analysis("vl_response_malformed")

        return parse_vl_response(raw_text)

    try:
        result = await vl_breaker.call(_do_call)
    except ServiceDegradedError:
        # 熔断状态：返回降级对象，不阻塞流程
        result = _fallback_analysis("vl_degraded")
    except httpx.RequestError as exc:
        logger.warning("analyze_image request error | exc=%s", exc)
        result = _fallback_analysis("vl_request_error")

    metrics.record_vl_parse_failure(result.parse_quality or "unknown")
    return result


def parse_vl_response(raw_text: str) -> ImageAnalysis:
    """三级容错解析 VL 返回文本为 ImageAnalysis。

    1. 从文本中抠出 JSON 并解析；
    2. 若 JSON 结构缺失字段，用 Pydantic 默认值补齐；
    3. 若完全无法解析，返回 fallback 对象并标记 parse_quality。
    """
    if not raw_text:
        return _fallback_analysis("empty_response")

    # 第一级：尝试提取 JSON 文本
    json_text = raw_text
    match = _JSON_BLOCK_RE.search(raw_text)
    if match:
        json_text = match.group(1).strip()
    else:
        # 没有代码块时，找第一个 { 到最后一个 }
        start = json_text.find("{")
        end = json_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            json_text = json_text[start : end + 1]

    # 第二级：解析并补齐
    try:
        data = json.loads(json_text)
        if not isinstance(data, dict):
            raise ValueError("parsed JSON is not an object")
        analysis = ImageAnalysis.model_validate(data)
        analysis.parse_quality = "ok"
        return analysis
    except (json.JSONDecodeError, ValueError) as exc:
        logger.info("parse_vl_response JSON decode failed, trying regex fallback | exc=%s", exc)
        return _regex_fallback(raw_text)


def _regex_fallback(raw_text: str) -> ImageAnalysis:
    """正则兜底：从非 JSON 文本中尽量提取关键字段。"""
    analysis = _fallback_analysis("fallback")

    # 简单启发式：若文本提到"人"，人数至少为 1
    if any(k in raw_text for k in ("人", "人物", "小孩", "老人", "青年")):
        analysis.persons.count = max(1, analysis.persons.count)

    # 尝试找 scene 关键词
    scenes = ["室内", "户外", "餐厅", "景区", "街道", "居家", "办公室", "车内"]
    for scene in scenes:
        if scene in raw_text:
            analysis.scene = scene
            break

    # 把原始文本本身作为 summary，保证 embedding 有东西可算
    cleaned = raw_text.replace("\n", " ").strip()
    analysis.summary = cleaned[:120] if cleaned else "图片内容无法结构化解析"
    analysis.parse_quality = "fallback"
    return analysis


def _fallback_analysis(reason: str) -> ImageAnalysis:
    """完全失败时的安全默认值。"""
    return ImageAnalysis(
        scene="unknown",
        scene_detail=None,
        summary="图片内容识别失败或响应异常",
        parse_quality=reason,
    )


async def embed_text(text: str) -> list[float]:
    """调 text-embedding-v3 得到 1024 维向量。"""
    if _is_mock():
        # 确定性随机数（同样的输入永远同样的输出），L2 归一化
        rng = random.Random(_stable_mock_seed(text))
        v = [rng.gauss(0, 1) for _ in range(1024)]
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    async def _do_call() -> list[float]:
        payload = {
            "model": settings.qwen_embedding_model,
            "input": {"texts": [text]},
            "parameters": {"text_type": "document"},
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=_EMB_TIMEOUT) as client:
            resp = await client.post(_EMB_URL, json=payload, headers=headers)

        if resp.status_code != 200:
            raise RuntimeError(
                f"DashScope Embedding HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()

        try:
            embeddings = data["output"]["embeddings"]
            vec = embeddings[0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"DashScope Embedding unexpected response: {data}"
            ) from exc

        if not vec or len(vec) != 1024:
            raise RuntimeError(
                f"Embedding dimension mismatch: got {len(vec) if vec else 0}, expected 1024"
            )
        return vec

    return await embedding_breaker.call(_do_call)


async def embed_query(text: str) -> list[float]:
    """检索用的向量。DashScope 建议查询侧 text_type=query。"""
    if _is_mock():
        return await embed_text(text)

    async def _do_call() -> list[float]:
        payload = {
            "model": settings.qwen_embedding_model,
            "input": {"texts": [text]},
            "parameters": {"text_type": "query"},
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=_EMB_TIMEOUT) as client:
            resp = await client.post(_EMB_URL, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(
                f"DashScope Embedding(query) HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        return data["output"]["embeddings"][0]["embedding"]

    return await embedding_breaker.call(_do_call)


def is_mock() -> bool:
    """给上层判断是否走 mock 用（比如日志、健康检查）。"""
    return _is_mock()
