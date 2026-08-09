"""图像生成抽象层：统一 wanx2.1-imageedit (通义万相) 和 gpt-image-2 (OpenAI)。

对外只暴露一个函数 generate(source_image_url, prompt, refs, model) → 统一生成结果。
新增模型时只需实现 _generate_<model_name> 私有函数并注册到 _REGISTRY。

设计要点
--------
- **异步轮询模式**：万相是异步 API，先创建任务拿 task_id，之后轮询直到 SUCCEEDED。
- **输入能力如实区分**：万相 2.1 接收一张原图；gpt-image-2 可额外接收参考图。
- **未配置时走 mock**：dev 环境跑通链路不需要真实计费。
- **wanx-v1 已弃用**：在 generate() 入口自动重定向到 wanx2.1-imageedit。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# ---- 常量 ------------------------------------------------------------
_WANX_CREATE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"
)
_WANX_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
_WANX_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_WANX_POLL_INTERVAL = 3.0
_WANX_MAX_POLLS = 40           # 3s * 40 = 120s

_GPT_IMAGE_URL = "https://api.openai.com/v1/images/edits"
_GPT_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


# ---- 数据结构 --------------------------------------------------------
@dataclass(slots=True)
class GenResult:
    cost_yuan: float      # 估算成本
    model: str
    image_url: str | None = None
    image_bytes: bytes | None = None
    content_type: str = "image/jpeg"


class GenerationError(RuntimeError):
    """生成失败统一异常."""


# ---- 是否走 mock ----------------------------------------------------
def _is_dashscope_mock() -> bool:
    return not settings.dashscope_api_key or settings.dashscope_api_key.strip() in (
        "",
        "sk-xxx",
    )


def _is_openai_mock() -> bool:
    # OpenAI Key 通过额外环境变量 OPENAI_API_KEY 传（可选）
    key = getattr(settings, "openai_api_key", "") or ""
    return not key or key == "sk-openai-xxx"


# ---- 对外总入口 ------------------------------------------------------
async def generate(
    source_image_url: str,
    prompt: str,
    reference_urls: list[str] | None = None,
    model: str = "wanx2.1-imageedit",
    function: str = "description_edit",
    strength: float = 0.7,
) -> GenResult:
    """
    对现有照片做 AI 改造。

    Parameters
    ----------
    source_image_url : 原图公网 URL（sign_get_url 拿到的）
    prompt           : 生图指令，已经把 Skill 模板渲染好的完整 prompt
    reference_urls   : 参考图公网 URL 列表；仅 gpt-image-2 支持额外参考图
    model            : "wanx2.1-imageedit" | "wanx-v1" | "gpt-image-2" | "mock"
    function         : wanx2.1-imageedit 功能模式
                       - description_edit: 指令编辑（简单修改，如换发色、加配饰）
                       - stylization_all: 全局风格化（整图风格迁移，仅支持法国绘本/金箔艺术）
                       - stylization_local: 局部风格化（指定区域风格化）
    strength         : 修改幅度 0.0~1.0，值越大风格变化越强烈（默认 0.7）
    """
    reference_urls = reference_urls or []
    # wanx-v1 已弃用，统一重定向到 wanx2.1-imageedit
    if model == "wanx-v1":
        model = "wanx2.1-imageedit"
    _wanx_models = {"wanx2.1-imageedit"}
    if model == "mock" or (model in _wanx_models and _is_dashscope_mock()):
        return await _generate_mock(source_image_url, prompt)
    if model in _wanx_models:
        if reference_urls:
            logger.warning(
                "wanx2.1-imageedit does not support extra reference images; ignored=%d",
                len(reference_urls),
            )
        return await _generate_wanx(
            source_image_url, prompt, model, function, strength
        )
    if model == "gpt-image-2":
        if _is_openai_mock():
            return await _generate_mock(source_image_url, prompt)
        return await _generate_gpt_image(source_image_url, prompt, reference_urls)
    raise GenerationError(f"Unsupported model: {model}")


# ---- mock 实现（返回原图，只是占位） ---------------------------------
async def _generate_mock(source_image_url: str, prompt: str) -> GenResult:
    logger.info("[mock] generate prompt=%r", prompt[:60])
    await asyncio.sleep(1.0)  # 模拟点延迟
    return GenResult(image_url=source_image_url, cost_yuan=0.0, model="mock")


# ---- 通义万相图生图 --------------------------------------------------
async def _generate_wanx(
    source_image_url: str,
    prompt: str,
    model: str = "wanx2.1-imageedit",
    function: str = "description_edit",
    strength: float = 0.7,
) -> GenResult:
    """
    调万相 image2image。异步任务：POST 创建 → 轮询 GET 任务状态。

    使用 wanx2.1-imageedit API 格式（function + base_image_url）。
    function 可选：
    - description_edit: 指令编辑（适合简单修改 + 风格描述）
    - stylization_all: 全局风格化（仅支持法国绘本/金箔艺术两种内置风格）
    - stylization_local: 局部风格化（8 种材质风格：ice/cloud/wooden 等）
    """
    # 构建 parameters：stylization_all 和 description_edit 支持 strength
    parameters: dict[str, Any] = {
        "n": 1,
        "watermark": False,
    }
    if function in ("stylization_all", "description_edit"):
        parameters["strength"] = strength

    payload: dict[str, Any] = {
        "model": "wanx2.1-imageedit",
        "input": {
            "prompt": prompt[:800],            # 800 字上限
            "function": function,               # 功能模式（可配置）
            "base_image_url": source_image_url,  # 原图
        },
        "parameters": parameters,
    }
    cost = 0.14

    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
        # 异步任务模式的必需 header
        "X-DashScope-Async": "enable",
    }

    logger.info(
        "wanx create | model=%s prompt=%r url=%s",
        model, prompt[:80], source_image_url,
    )

    async with httpx.AsyncClient(timeout=_WANX_TIMEOUT) as client:
        resp = await client.post(_WANX_CREATE_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise GenerationError(f"wanx create HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    task_id = data.get("output", {}).get("task_id")
    if not task_id:
        raise GenerationError(f"wanx no task_id: {data}")
    logger.info("wanx task created: %s (model=%s)", task_id, model)

    # ---- 轮询 ----
    result_url = await _poll_wanx_task(task_id)
    return GenResult(image_url=result_url, cost_yuan=cost, model=model)


async def _poll_wanx_task(task_id: str) -> str:
    url = _WANX_TASK_URL.format(task_id=task_id)
    headers = {"Authorization": f"Bearer {settings.dashscope_api_key}"}

    async with httpx.AsyncClient(timeout=_WANX_TIMEOUT) as client:
        for i in range(_WANX_MAX_POLLS):
            await asyncio.sleep(_WANX_POLL_INTERVAL)
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise GenerationError(
                    f"wanx poll HTTP {resp.status_code}: {resp.text[:300]}"
                )
            data = resp.json()
            output = data.get("output", {})
            status = output.get("task_status")
            logger.debug("wanx poll #%d status=%s", i, status)
            if status == "SUCCEEDED":
                results = output.get("results") or []
                if not results or not results[0].get("url"):
                    raise GenerationError(f"wanx no result url: {data}")
                return results[0]["url"]
            if status in ("FAILED", "CANCELED"):
                raise GenerationError(
                    f"wanx task {status}: {output.get('message', data)}"
                )
    raise GenerationError("wanx task timeout")


# ---- OpenAI gpt-image-2 -------------------------------------------------
async def _generate_gpt_image(
    source_image_url: str,
    prompt: str,
    reference_urls: list[str],
) -> GenResult:
    """
    调 OpenAI images/edits。请求体是 multipart/form-data。
    需要先把 source + refs 下载成 bytes。
    """
    key = getattr(settings, "openai_api_key", "") or ""
    if not key:
        raise GenerationError("OPENAI_API_KEY not configured")

    async with httpx.AsyncClient(timeout=_GPT_TIMEOUT) as client:
        image_parts = []
        for index, url in enumerate([source_image_url, *reference_urls[:4]]):
            image_resp = await client.get(url)
            if image_resp.status_code != 200:
                raise GenerationError(
                    f"download input image HTTP {image_resp.status_code}: {url}"
                )
            content_type = _image_content_type(image_resp)
            suffix = _IMAGE_SUFFIXES[content_type]
            image_parts.append(
                (
                    "image[]",
                    (f"input-{index}{suffix}", image_resp.content, content_type),
                )
            )

        resp = await client.post(
            _GPT_IMAGE_URL,
            files=image_parts,
            data={
                "prompt": prompt[:1000],
                "model": "gpt-image-2",
                "n": 1,
                "size": "1024x1024",
            },
            headers={"Authorization": f"Bearer {key}"},
        )
    if resp.status_code != 200:
        raise GenerationError(
            f"gpt-image HTTP {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    try:
        encoded = data["data"][0]["b64_json"]
    except (KeyError, IndexError) as exc:
        raise GenerationError(f"gpt-image unexpected: {data}") from exc
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise GenerationError("gpt-image returned invalid base64 image data") from exc
    if not image_bytes:
        raise GenerationError("gpt-image returned an empty image")
    return GenResult(
        cost_yuan=0.30,
        model="gpt-image-2",
        image_bytes=image_bytes,
        content_type="image/png",
    )


_IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _image_content_type(response: httpx.Response) -> str:
    """提取 OpenAI 支持的输入图片类型，缺失时按 JPEG 处理。"""
    content_type = response.headers.get("content-type", "image/jpeg")
    content_type = content_type.partition(";")[0].strip().lower()
    return content_type if content_type in _IMAGE_SUFFIXES else "image/jpeg"
