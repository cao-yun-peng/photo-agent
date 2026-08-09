"""Skill Prompt 自动化测试脚本。

功能：
  1. 对每个 Skill × 测试图片 × 模型 组合调生成 API
  2. 用 qwen-vl 对生成结果自动评分（4 维度 1-5 分）
  3. 输出 HTML 对比报告（含图片、分数、通过/不通过判定）

用法：
  # 基本用法：提供测试图片 URL
  python scripts/prompt_test.py \
    --images https://example.com/photo1.jpg https://example.com/photo2.jpg

  # 只测新 Skill（不测基线）
  python scripts/prompt_test.py --images URL1 URL2 --new-only

  # 只测 wanx-v1 模型
  python scripts/prompt_test.py --images URL1 --models wanx-v1

  # 指定输出路径
  python scripts/prompt_test.py --images URL1 --output reports/test_001.html

  # 并发控制（默认 2 路并发）
  python scripts/prompt_test.py --images URL1 URL2 URL3 --concurrency 3

前置条件：
  - .env 中配置 DASHSCOPE_API_KEY（必须）和 OPENAI_API_KEY（测 gpt-image-2 时需要）
  - pip install httpx 已在项目依赖中

评分标准：
  - 风格辨识度 ≥ 4，4 项平均分 ≥ 3.5，无任何项 ≤ 2 → PASS
  - 否则 → FAIL，报告中会高亮标注
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# 让 import 能找到 app 包
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings
from scripts.test_skills_config import (
    EXISTING_SKILLS,
    NEW_SKILLS,
    PASS_THRESHOLD,
    SCORE_DIMENSIONS,
)

logger = logging.getLogger(__name__)

# ---- API 常量 -----------------------------------------------------------
WANX_CREATE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"
)
WANX_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
VL_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
GPT_IMAGE_URL = "https://api.openai.com/v1/images/edits"

TIMEOUT = httpx.Timeout(120.0, connect=10.0)
POLL_INTERVAL = 3.0
MAX_POLLS = 40


# ---- 数据结构 -----------------------------------------------------------
@dataclass
class TestResult:
    """一次生成 + 评分的完整结果。"""
    skill_name: str
    skill_category: str
    is_new: bool                    # True=新 Skill, False=基线
    model: str                      # wanx-v1 / gpt-image-2
    image_url: str                  # 原图 URL
    prompt: str                     # 实际发送的 prompt
    generated_url: str = ""         # 生成结果 URL
    scores: dict[str, int] = field(default_factory=dict)
    avg_score: float = 0.0
    passed: bool = False
    error: str = ""
    cost_yuan: float = 0.0
    elapsed_sec: float = 0.0
    vl_comment: str = ""            # VL 评分的额外说明


# ---- 生成函数 -----------------------------------------------------------
async def generate_wanx(
    client: httpx.AsyncClient,
    source_url: str,
    prompt: str,
    negative_prompt: str = "",
) -> tuple[str, float]:
    """调 wanx-v1 图生图，返回 (result_url, cost)。"""
    payload: dict[str, Any] = {
        "model": "wanx-v1",
        "input": {
            "prompt": prompt[:500],
            "ref_img": source_url,
        },
        "parameters": {
            "style": "<auto>",
            "size": "1024*1024",
            "n": 1,
        },
    }
    if negative_prompt:
        payload["input"]["negative_prompt"] = negative_prompt[:200]

    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    resp = await client.post(WANX_CREATE_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"wanx create HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    task_id = data.get("output", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"wanx no task_id: {data}")

    # 轮询
    poll_headers = {"Authorization": f"Bearer {settings.dashscope_api_key}"}
    for _ in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL)
        poll_resp = await client.get(
            WANX_TASK_URL.format(task_id=task_id), headers=poll_headers
        )
        poll_data = poll_resp.json()
        status = poll_data.get("output", {}).get("task_status")
        if status == "SUCCEEDED":
            results = poll_data.get("output", {}).get("results", [])
            if results and results[0].get("url"):
                return results[0]["url"], 0.14
            raise RuntimeError(f"wanx no result url: {poll_data}")
        if status in ("FAILED", "CANCELED"):
            msg = poll_data.get("output", {}).get("message", str(poll_data))
            raise RuntimeError(f"wanx task {status}: {msg}")
    raise RuntimeError("wanx task timeout")


async def generate_gpt_image(
    client: httpx.AsyncClient,
    source_url: str,
    prompt: str,
) -> tuple[str, float]:
    """调 gpt-image-2 图生图，返回 (result_url, cost)。"""
    key = settings.openai_api_key or ""
    if not key or key == "sk-openai-xxx":
        raise RuntimeError("OPENAI_API_KEY not configured")

    # 下载原图
    img_resp = await client.get(source_url)
    if img_resp.status_code != 200:
        raise RuntimeError(f"download source HTTP {img_resp.status_code}")

    files = [("image", ("source.jpg", img_resp.content, "image/jpeg"))]
    resp = await client.post(
        GPT_IMAGE_URL,
        files=files,
        data={
            "prompt": prompt[:1000],
            "model": "gpt-image-1",
            "n": 1,
            "size": "1024x1024",
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"gpt-image HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["data"][0]["url"], 0.30
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"gpt-image unexpected: {data}") from exc


# ---- 评分函数 -----------------------------------------------------------
SCORE_PROMPT_TEMPLATE = """你是一个专业的 AI 图像质量评审员。

请对比【原图】和【AI 改造后的图】，按照以下 4 个维度分别打分（1-5 分）：

1. 风格辨识度：改造后的图是否明显呈现了「{skill_name}」的风格？
2. 主体保真度：原图中的主要人物/物体在改造后是否还能辨认出来？
3. 画面美观度：色彩、构图、清晰度是否好看？
4. 无瑕疵度：是否有变形、模糊、多余元素、文字水印等瑕疵？

请严格按以下 JSON 格式输出，不要输出其他内容：
{{"style": 分数, "fidelity": 分数, "quality": 分数, "clean": 分数, "comment": "一句话评价"}}"""


async def score_image(
    client: httpx.AsyncClient,
    source_url: str,
    generated_url: str,
    skill_name: str,
) -> dict[str, Any]:
    """用 qwen-vl 对生成结果评分。"""
    prompt = SCORE_PROMPT_TEMPLATE.format(skill_name=skill_name)

    payload: dict[str, Any] = {
        "model": settings.qwen_vl_model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": source_url},
                        {"image": generated_url},
                        {"text": prompt},
                    ],
                }
            ]
        },
        "parameters": {
            "result_format": "message",
            "max_tokens": 300,
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.dashscope_api_key}",
        "Content-Type": "application/json",
    }

    resp = await client.post(VL_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"VL score HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        choices = data["output"]["choices"]
        content = choices[0]["message"]["content"]
        if isinstance(content, list):
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        else:
            text = str(content)
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"VL unexpected: {data}") from exc

    # 解析 JSON（容忍 markdown 代码块包裹）
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # 尝试从文本中提取 JSON
        import re
        match = re.search(r'\{[^}]+\}', text)
        if match:
            result = json.loads(match.group())
        else:
            raise RuntimeError(f"VL score parse failed: {text[:200]}")

    # 校验分数范围
    for key in ("style", "fidelity", "quality", "clean"):
        val = result.get(key, 3)
        result[key] = max(1, min(5, int(val)))
    result.setdefault("comment", "")

    return result


# ---- 单次测试 -----------------------------------------------------------
async def run_single_test(
    client: httpx.AsyncClient,
    skill: dict,
    image_url: str,
    model: str,
    is_new: bool,
    semaphore: asyncio.Semaphore,
) -> TestResult:
    """跑一个 skill × image × model 的完整测试。"""
    result = TestResult(
        skill_name=skill["name"],
        skill_category=skill.get("category", ""),
        is_new=is_new,
        model=model,
        image_url=image_url,
        prompt=skill["prompt_template"],
    )

    async with semaphore:
        t0 = time.monotonic()
        try:
            logger.info(
                "[生成] %s × %s (%s)", skill["name"], _short_url(image_url), model
            )
            if model == "wanx-v1":
                gen_url, cost = await generate_wanx(
                    client, image_url, skill["prompt_template"],
                    skill.get("negative_prompt", ""),
                )
            elif model == "gpt-image-2":
                gen_url, cost = await generate_gpt_image(
                    client, image_url, skill["prompt_template"],
                )
            else:
                raise RuntimeError(f"unsupported model: {model}")

            result.generated_url = gen_url
            result.cost_yuan = cost
            logger.info(
                "[评分] %s × %s (%s)", skill["name"], _short_url(image_url), model
            )

            scores = await score_image(
                client, image_url, gen_url, skill["name"]
            )
            result.scores = {
                k: scores[k] for k in ("style", "fidelity", "quality", "clean")
            }
            result.vl_comment = scores.get("comment", "")
            vals = list(result.scores.values())
            result.avg_score = sum(vals) / len(vals) if vals else 0

            # 判定通过
            result.passed = (
                result.avg_score >= PASS_THRESHOLD["min_avg"]
                and min(vals) > PASS_THRESHOLD["min_any"]
                and result.scores.get("style", 0) >= PASS_THRESHOLD["min_style"]
            )

        except Exception as exc:
            result.error = str(exc)[:300]
            logger.error(
                "[失败] %s × %s: %s",
                skill["name"], _short_url(image_url), exc,
            )

        result.elapsed_sec = time.monotonic() - t0

    return result


# ---- HTML 报告生成 -------------------------------------------------------
def generate_report(results: list[TestResult], output_path: Path) -> None:
    """生成 HTML 对比报告。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 按 skill 分组
    skill_groups: dict[str, list[TestResult]] = {}
    for r in results:
        key = f"{'[新] ' if r.is_new else '[基线] '}{r.skill_name}"
        skill_groups.setdefault(key, []).append(r)

    # 统计
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if r.error)
    total_cost = sum(r.cost_yuan for r in results)

    # 构建 HTML
    cards_html = []
    for group_name, group_results in skill_groups.items():
        is_new = group_results[0].is_new
        badge_class = "badge-new" if is_new else "badge-base"
        badge_text = "新增" if is_new else "基线"

        rows_html = []
        for r in group_results:
            if r.error:
                score_cells = f'<td colspan="5" class="error-cell">{_esc(r.error)}</td>'
            else:
                score_cells = ""
                for dim in SCORE_DIMENSIONS:
                    val = r.scores.get(dim["key"], 0)
                    color = _score_color(val)
                    score_cells += (
                        f'<td style="color:{color};font-weight:700">{val}</td>'
                    )
                status = (
                    '<span class="pass">PASS</span>'
                    if r.passed
                    else '<span class="fail">FAIL</span>'
                )
                score_cells += f"<td>{status}</td>"

            rows_html.append(f"""<tr>
                <td class="model-cell">{_esc(r.model)}</td>
                <td class="img-cell">
                    <a href="{_esc(r.image_url)}" target="_blank">
                        <img src="{_esc(r.image_url)}" alt="原图" loading="lazy">
                    </a>
                </td>
                <td class="img-cell">
                    {"<a href='" + _esc(r.generated_url) + "' target='_blank'><img src='" + _esc(r.generated_url) + "' alt='生成' loading='lazy'></a>" if r.generated_url else "—"}
                </td>
                {score_cells}
                <td>{r.avg_score:.1f}</td>
                <td class="comment-cell">{_esc(r.vl_comment)}</td>
                <td>{r.elapsed_sec:.1f}s</td>
            </tr>""")

        # Skill 级汇总
        group_scores = [r.avg_score for r in group_results if r.avg_score > 0]
        group_avg = sum(group_scores) / len(group_scores) if group_scores else 0
        group_passed = sum(1 for r in group_results if r.passed)
        group_total = len(group_results)

        cards_html.append(f"""
        <div class="skill-card {'card-pass' if group_avg >= PASS_THRESHOLD['min_avg'] else 'card-fail'}">
            <div class="card-header">
                <span class="badge {badge_class}">{badge_text}</span>
                <h3>{_esc(group_name.split('] ')[1])}</h3>
                <span class="card-summary">
                    均分 <strong>{group_avg:.1f}</strong> ·
                    通过 {group_passed}/{group_total}
                </span>
            </div>
            <div class="card-desc">{_esc(group_results[0].skill_category)} · {_esc(group_results[0].prompt[:80])}...</div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>模型</th><th>原图</th><th>生成</th>
                        <th>风格</th><th>保真</th><th>美观</th><th>无瑕</th>
                        <th>结果</th><th>均分</th><th>评价</th><th>耗时</th>
                    </tr></thead>
                    <tbody>{"".join(rows_html)}</tbody>
                </table>
            </div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prompt 测试报告</title>
<style>
:root {{
    --bg: #0f0f14; --bg2: #1a1a24; --bg3: #22223a;
    --ink: #e8e6f0; --muted: #8b89a0; --rule: #2d2d42;
    --accent: #a78bfa; --accent2: #f472b6;
    --success: #34d399; --danger: #f87171; --warn: #fbbf24;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--ink);
    font-size: 14px; line-height: 1.6;
}}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
h1 {{
    font-size: 28px; margin-bottom: 8px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}}
.meta {{ color: var(--muted); font-size: 13px; margin-bottom: 24px; }}
.stats {{
    display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap;
}}
.stat {{
    background: var(--bg2); border: 1px solid var(--rule);
    border-radius: 10px; padding: 16px 24px; min-width: 120px;
}}
.stat-val {{
    font-size: 28px; font-weight: 700;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}}
.stat-label {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
.skill-card {{
    background: var(--bg2); border: 1px solid var(--rule);
    border-radius: 14px; padding: 24px; margin-bottom: 20px;
}}
.card-pass {{ border-left: 4px solid var(--success); }}
.card-fail {{ border-left: 4px solid var(--danger); }}
.card-header {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
}}
.card-header h3 {{ font-size: 18px; }}
.card-summary {{
    margin-left: auto; font-size: 13px; color: var(--muted);
}}
.card-desc {{
    font-size: 12px; color: var(--muted); margin-bottom: 16px;
    font-family: monospace; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis;
}}
.badge {{
    font-size: 11px; padding: 2px 10px; border-radius: 10px;
    font-weight: 600;
}}
.badge-new {{ color: var(--accent2); background: rgba(244,114,182,0.15); }}
.badge-base {{ color: var(--accent); background: rgba(167,139,250,0.15); }}
.table-wrap {{
    overflow-x: auto; overflow-y: auto; max-height: 500px;
    border: 1px solid var(--rule); border-radius: 8px;
}}
table {{
    width: 100%; border-collapse: collapse; min-width: 900px;
    font-size: 13px;
}}
thead {{ position: sticky; top: 0; z-index: 2; }}
th {{
    background: var(--bg3); color: var(--accent);
    font-weight: 600; padding: 10px 12px; text-align: left;
    border-bottom: 2px solid var(--rule); font-size: 12px;
}}
td {{
    padding: 8px 12px; border-bottom: 1px solid var(--rule);
    vertical-align: middle;
}}
.model-cell {{ font-weight: 600; white-space: nowrap; }}
.img-cell {{ min-width: 80px; }}
.img-cell img {{
    width: 80px; height: 80px; object-fit: cover;
    border-radius: 6px; border: 1px solid var(--rule);
    cursor: pointer; transition: transform 0.2s;
}}
.img-cell img:hover {{ transform: scale(1.5); z-index: 10; position: relative; }}
.comment-cell {{
    max-width: 200px; font-size: 12px; color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.error-cell {{
    color: var(--danger); font-size: 12px; font-family: monospace;
    word-break: break-all;
}}
.pass {{
    color: var(--success); font-weight: 700;
    background: rgba(52,211,153,0.1); padding: 2px 8px;
    border-radius: 4px; font-size: 12px;
}}
.fail {{
    color: var(--danger); font-weight: 700;
    background: rgba(248,113,113,0.1); padding: 2px 8px;
    border-radius: 4px; font-size: 12px;
}}
footer {{
    text-align: center; padding: 32px 0; color: var(--muted);
    font-size: 12px; border-top: 1px solid var(--rule); margin-top: 32px;
}}
</style>
</head>
<body>
<div class="container">
    <h1>Prompt 测试报告</h1>
    <div class="meta">生成时间: {now} · 测试图片 {len(set(r.image_url for r in results))} 张 · 模型 {', '.join(sorted(set(r.model for r in results)))}</div>

    <div class="stats">
        <div class="stat">
            <div class="stat-val">{total}</div>
            <div class="stat-label">总测试数</div>
        </div>
        <div class="stat">
            <div class="stat-val" style="background:linear-gradient(135deg,var(--success),#6ee7b7);-webkit-background-clip:text">{passed}</div>
            <div class="stat-label">通过</div>
        </div>
        <div class="stat">
            <div class="stat-val" style="background:linear-gradient(135deg,var(--danger),#fca5a5);-webkit-background-clip:text">{failed}</div>
            <div class="stat-label">失败</div>
        </div>
        <div class="stat">
            <div class="stat-val">{total_cost:.2f}</div>
            <div class="stat-label">总成本 (元)</div>
        </div>
    </div>

    {"".join(cards_html)}
</div>
<footer>photo-agent prompt_test.py · 自动化 Skill 质量评估</footer>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("报告已生成: %s", output_path)


# ---- 工具函数 -----------------------------------------------------------
def _short_url(url: str) -> str:
    """截短 URL 用于日志。"""
    if len(url) > 60:
        return url[:30] + "..." + url[-20:]
    return url


def _esc(text: str) -> str:
    """HTML 转义。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _score_color(val: int) -> str:
    if val >= 4:
        return "var(--success)"
    if val >= 3:
        return "var(--warn)"
    return "var(--danger)"


# ---- 主流程 -------------------------------------------------------------
async def main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 确定测试的 Skills
    skills_to_test: list[tuple[dict, bool]] = []
    if not args.base_only:
        for s in NEW_SKILLS:
            skills_to_test.append((s, True))
    if not args.new_only:
        for s in EXISTING_SKILLS:
            skills_to_test.append((s, False))

    # 确定模型
    models = args.models or ["wanx-v1"]

    # 总任务数
    total = len(skills_to_test) * len(args.images) * len(models)
    logger.info(
        "测试计划: %d 个 Skill × %d 张图 × %d 个模型 = %d 次生成",
        len(skills_to_test), len(args.images), len(models), total,
    )
    logger.info("预估成本: %.2f - %.2f 元", total * 0.14, total * 0.30)
    logger.info("预估时间: %d - %d 分钟", total // 5, total // 2)

    semaphore = asyncio.Semaphore(args.concurrency)
    results: list[TestResult] = []

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        tasks = []
        for skill, is_new in skills_to_test:
            for image_url in args.images:
                for model in models:
                    tasks.append(
                        run_single_test(
                            client, skill, image_url, model, is_new, semaphore
                        )
                    )

        # 按顺序执行（保证报告有序），但 semaphore 控制并发
        for i, task in enumerate(tasks):
            result = await task
            results.append(result)
            done = i + 1
            logger.info(
                "进度: %d/%d (%.0f%%)", done, total, done / total * 100
            )

    # 生成报告
    output_path = Path(args.output)
    generate_report(results, output_path)

    # 打印摘要
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if r.error)
    print(f"\n{'='*50}")
    print(f"测试完成: {passed}/{len(results)} 通过, {failed} 失败")
    print(f"报告路径: {output_path}")
    print(f"{'='*50}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Skill Prompt 自动化测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--images", nargs="+", required=True,
        help="测试图片 URL 列表（需公网可访问）",
    )
    parser.add_argument(
        "--models", nargs="+", default=None,
        choices=["wanx-v1", "gpt-image-2"],
        help="测试的模型列表（默认 wanx-v1）",
    )
    parser.add_argument(
        "--new-only", action="store_true",
        help="只测试新增 Skill（不测基线）",
    )
    parser.add_argument(
        "--base-only", action="store_true",
        help="只测试基线 Skill（不测新增）",
    )
    parser.add_argument(
        "--output", default=None,
        help="报告输出路径（默认 reports/prompt_test_YYYYMMDD_HHMMSS.html）",
    )
    parser.add_argument(
        "--concurrency", type=int, default=2,
        help="并发数（默认 2）",
    )
    args = parser.parse_args()

    if args.output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = str(ROOT / "reports" / f"prompt_test_{ts}.html")

    return args


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
