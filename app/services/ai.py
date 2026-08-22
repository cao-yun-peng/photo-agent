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
_VL_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_EMB_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"

# 让 worker 有个合理超时；VL 慢一些，Embedding 快
_VL_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_EMB_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# 生成描述的 Prompt。改这里就等于改产品口吻。
_VL_PROMPT = (
    "请用一段简洁、可检索的中文描述这张图片。除主要人物/物体、场景和氛围外，"
    "优先写出画面中可见的具体动作、可辨认年龄段、模糊类型、拍摄载体/视角，以及"
    "主体在画面中的位置和朝向；看不清就省略，不要猜测。控制在 100 字以内，不要使用列表或标题。"
)

# 结构化分析 Prompt：v5 增加可用于完整集合扫描的稳定语义字段。
VL_ANALYSIS_PROMPT_VERSION = "v5"
_VL_ANALYSIS_PROMPT = """请分析这张图片，并且只返回一个合法 JSON 对象，不要输出解释或 Markdown：
{
  "scene": "受控场景标签",
  "scene_detail": "更具体的地点、环境和拍摄视角",
  "persons": {
    "count": 0,
    "age_estimate": "可选",
    "expression": "可选"
  },
  "photo_type": "selfie/screenshot/group_photo/portrait/document/food/scenery/other",
  "is_selfie": false,
  "people_count": 0,
  "actions": ["主体正在进行的、画面可见的具体动作"],
  "age_groups": ["儿童/青年/中年/老年"],
  "blur_type": "无明显模糊/运动模糊/失焦/相机抖动/镜头雾化/隔窗模糊/混合模糊/无法判断",
  "capture_context": ["拍摄载体、视角或介质"],
  "spatial_layout": ["主体位置、朝向和相对关系"],
  "distinctive_details": ["用于区分近似照片的可见细节"],
  "objects": ["清晰可见、适合搜索的具体实体标签"],
  "text_in_image": ["逐条抄录清晰可辨的文字"],
  "mood": "氛围词，可选",
  "colors": ["主要颜色"],
  "summary": "80–120 字以内的中文内容摘要"
}

严格规则：
1. scene 必须且只能从下列值中选择一个，不得组合、扩写或自造标签：室内、户外、居家、办公室、餐厅、便利店、商店、教室、厨房、卧室、客厅、机场、车内、街道、公园、景区、海边、沙滩、球场、校园、门廊、阳台、婚礼、其他。更具体的自由文本只能写入 scene_detail。
2. persons.count 只统计主体场景中能清楚辨认为人的真实人物。排除电视/电脑屏幕、海报、照片中的人物，也排除孤立肢体和很小、模糊的背景人影；多人合影要逐个计数。
3. photo_type 必须且只能从 selfie、screenshot、group_photo、portrait、document、food、scenery、other 中选择一个。自拍优先标 selfie；手机或电脑界面截图标 screenshot；以两名及以上人物合影为主体标 group_photo；以单人为主体但不是自拍标 portrait。
4. people_count 必须与 persons.count 完全一致；is_selfie 仅在画面呈现自拍视角、镜面自拍或明显手持前置拍摄时为 true，并同时令 photo_type=selfie、capture_context 包含“自拍”。
5. objects 列出清晰可见且有检索价值的具体实体，通常 5–12 个；优先使用具体名称，避免只写宠物、食物、建筑、交通工具等泛词，但不要猜测看不清的物体。
6. persons.count 大于 0 时，objects 中加入“人物”；能判断身份时再加入学生、教师、新娘、运动员等角色标签。
7. text_in_image 检查招牌、屏幕、包装、衣服、票据和手写文字，按可见内容逐条抄录，保留中英文和数字，不要翻译或编造。
8. actions 只写肉眼可见的动作，例如跑动、拥抱、挥手、端杯、看向左侧；不要把身份、情绪或推测目的写成动作。
9. age_groups 只能使用儿童、青年、中年、老年；无法可靠判断时返回空数组，不得根据场景或衣着臆测。
10. blur_type 必须从给定值中选一个。区分主体运动拖影、整体失焦、相机抖动、镜头雾化与隔着玻璃造成的模糊。
11. capture_context 可使用汽车内、公交车内、火车内、隔窗拍摄、屏幕截图、自拍、俯拍、仰拍、特写、远景、正面拍摄等可见标签；无依据时返回空数组。
12. spatial_layout 写 1–6 条短语，覆盖左/中/右、前景/背景、面向/侧身/背影以及主体间相对位置；distinctive_details 写可区分相似图的服饰、姿态、局部物体或光线细节。
13. summary 必须优先综合可见的动作、年龄、模糊、拍摄载体和空间位置；未知信息直接省略。
14. 所有描述字段使用中文；所有复数字段必须是 JSON 字符串数组；persons.count 和 people_count 必须是整数。"""

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

        # DashScope is directly reachable in the supported runtime.  Ignoring the
        # host's desktop proxy prevents stale system proxy settings from turning
        # otherwise healthy model calls into connect timeouts.
        async with httpx.AsyncClient(timeout=_VL_TIMEOUT, trust_env=False) as client:
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
            raise RuntimeError(f"DashScope VL unexpected response: {data}") from exc

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
                "max_tokens": 1200,
            },
        }
        headers = {
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=_VL_TIMEOUT, trust_env=False) as client:
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
            logger.warning(
                "analyze_image unexpected response | exc=%s data=%s", exc, data
            )
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
        _normalize_person_fields(data)
        analysis = ImageAnalysis.model_validate(data)
        analysis.parse_quality = "ok"
        analysis.analysis_version = VL_ANALYSIS_PROMPT_VERSION
        return analysis
    except (json.JSONDecodeError, ValueError) as exc:
        logger.info(
            "parse_vl_response JSON decode failed, trying regex fallback | exc=%s", exc
        )
        return _regex_fallback(raw_text)


def _normalize_person_fields(data: dict[str, Any]) -> None:
    """兼容模型偶发返回 [] 的类型漂移，避免无谓 fallback。"""
    persons = data.get("persons")
    if not isinstance(persons, dict):
        return

    for key in ("age_estimate", "expression"):
        value = persons.get(key)
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            persons[key] = cleaned[0] if cleaned else None


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
        analysis_version=VL_ANALYSIS_PROMPT_VERSION,
    )


def build_retrieval_text(description: str, analysis: ImageAnalysis) -> str:
    """把结构化视觉证据展开为 embedding 文本，避免 JSON 新字段只存不搜。"""

    def _clean(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = str(value).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    parts: list[str] = []
    for value in (description, analysis.summary):
        value = (value or "").strip()
        if value and value not in parts:
            parts.append(value)

    scalar_fields = (
        ("照片类型", analysis.photo_type),
        ("是否自拍", "是" if analysis.is_selfie else "否"),
        ("人物数量", str(analysis.people_count)),
        ("场景", analysis.scene if analysis.scene != "unknown" else None),
        ("场景细节", analysis.scene_detail),
        ("模糊类型", analysis.blur_type),
        ("氛围", analysis.mood),
    )
    for label, value in scalar_fields:
        if value and str(value).strip():
            parts.append(f"{label}：{str(value).strip()}")

    list_fields = (
        ("动作", analysis.actions),
        ("年龄组", analysis.age_groups),
        ("拍摄方式", analysis.capture_context),
        ("空间位置", analysis.spatial_layout),
        ("区分细节", analysis.distinctive_details),
        ("物体", analysis.objects),
        ("画面文字", analysis.text_in_image),
        ("颜色", analysis.colors),
    )
    for label, values in list_fields:
        cleaned = _clean(values)
        if cleaned:
            parts.append(f"{label}：{'、'.join(cleaned)}")

    if analysis.persons.count:
        person_bits = [f"{analysis.persons.count}人"]
        if analysis.persons.age_estimate:
            person_bits.append(analysis.persons.age_estimate.strip())
        if analysis.persons.expression:
            person_bits.append(analysis.persons.expression.strip())
        parts.append(f"人物：{'，'.join(person_bits)}")

    return "\n".join(parts)[:2000]


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

        async with httpx.AsyncClient(timeout=_EMB_TIMEOUT, trust_env=False) as client:
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
        async with httpx.AsyncClient(timeout=_EMB_TIMEOUT, trust_env=False) as client:
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
