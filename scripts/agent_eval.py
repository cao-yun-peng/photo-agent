"""Photo Agent 对话能力评测运行器。

用法：
    # 使用真实 LLM 评测（需要 DashScope API Key）
    python scripts/agent_eval.py --dataset tests/eval/agent_eval_dataset.json --mode real

    # 使用 mock LLM 评测（确定性，不消耗 API 额度）
    python scripts/agent_eval.py --dataset tests/eval/agent_eval_dataset.json --mode mock

    # 指定维度评测
    python scripts/agent_eval.py --dataset tests/eval/agent_eval_dataset.json --dimensions D1,D2,D8

    # 指定优先级
    python scripts/agent_eval.py --dataset tests/eval/agent_eval_dataset.json --priority P0

评测维度：
    D1  意图识别     — Agent 能否正确判断用户意图
    D2  工具选择     — 是否在正确的场景调用正确的工具
    D3  参数构造     — 传给 Tool 的参数是否合理且完整
    D4  多步推理     — 能否正确串联多个 Tool
    D5  主动澄清     — 需求模糊时是否主动提出澄清
    D6  兜底策略     — 搜索无结果时是否触发三级兜底
    D7  边界处理     — 空相册、额度用尽等边界情况
    D8  安全意识     — 不越权、不误操作
    D9  上下文续接   — 多轮对话中能否正确续接
    D10 预算遵守     — 是否在预算限制内终止

评估指标：
    - tool_selection_accuracy: 工具选择准确率
    - tool_order_accuracy: 工具调用顺序准确率
    - must_not_call_violation_rate: 不应调用却调用的比例
    - parameter_accuracy: 参数构造准确率
    - final_status_accuracy: 最终状态准确率
    - safety_pass_rate: 安全用例通过率
    - budget_compliance_rate: 预算遵守率
    - dimension_score: 各维度加权得分
    - overall_score: 总体得分
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid5, NAMESPACE_DNS, uuid4

# 确保能 import app 包
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 设置测试环境变量（在 import app 之前）
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DASHSCOPE_API_KEY", "sk-xxx")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test_secret_for_eval")
os.environ.setdefault("OSS_BUCKET", "photo-agent-dev")
os.environ.setdefault("OSS_KEY_ID", "LTAI_xxx")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 评估结果数据结构
# ------------------------------------------------------------------
class TestCaseResult:
    """单个测试用例的评估结果。"""

    def __init__(self, test_id: str, dimension: str, priority: str) -> None:
        self.test_id = test_id
        self.dimension = dimension
        self.priority = priority
        self.tool_calls: list[dict[str, Any]] = []  # 实际工具调用记录
        self.final_status: str = ""
        self.final_message: str = ""
        self.elapsed_ms: int = 0
        self.steps: int = 0
        self.total_tokens: int = 0
        self.error: str | None = None

        # 评分
        self.score_tool_selection: float = 0.0
        self.score_tool_order: float = 0.0
        self.score_must_not_call: float = 1.0  # 默认通过，违规则扣分
        self.score_parameter: float = 0.0
        self.score_final_status: float = 0.0
        self.score_safety: float = 1.0  # 默认安全
        self.score_budget: float = 1.0  # 默认遵守预算
        self.score_overall: float = 0.0

        # 评估详情
        self.evaluation_notes: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "dimension": self.dimension,
            "priority": self.priority,
            "tool_calls": self.tool_calls,
            "final_status": self.final_status,
            "final_message": self.final_message[:500],
            "elapsed_ms": self.elapsed_ms,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "error": self.error,
            "scores": {
                "tool_selection": round(self.score_tool_selection, 3),
                "tool_order": round(self.score_tool_order, 3),
                "must_not_call": round(self.score_must_not_call, 3),
                "parameter": round(self.score_parameter, 3),
                "final_status": round(self.score_final_status, 3),
                "safety": round(self.score_safety, 3),
                "budget": round(self.score_budget, 3),
                "overall": round(self.score_overall, 3),
            },
            "notes": self.evaluation_notes,
        }


# ------------------------------------------------------------------
# 工具调用拦截器
# ------------------------------------------------------------------
class ToolCallInterceptor:
    """拦截 Agent 的所有 Tool 调用，记录名称和参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def make_wrapper(self, original_fn: Any, tool_name: str) -> Any:
        """包装原始 Tool 函数，在调用前记录参数。"""
        interceptor = self

        async def wrapped(**kwargs):
            # 记录调用
            call_record = {
                "tool": tool_name,
                "arguments": {k: str(v)[:200] for k, v in kwargs.items()},
                "timestamp": time.time(),
            }
            interceptor.calls.append(call_record)
            logger.info("  [TOOL] %s(%s)", tool_name, call_record["arguments"])

            # 调用原始函数
            result = await original_fn(**kwargs)
            call_record["result_ok"] = result.get("ok", False) if isinstance(result, dict) else True
            return result

        return wrapped

    def get_tool_sequence(self) -> list[str]:
        """返回工具调用顺序列表（不含 final_answer）。"""
        return [c["tool"] for c in self.calls if c["tool"] != "final_answer"]

    def get_all_tools_called(self) -> set[str]:
        """返回所有被调用的工具名称集合。"""
        return {c["tool"] for c in self.calls}


# ------------------------------------------------------------------
# Mock 相册数据构建
# ------------------------------------------------------------------
def _photo_id_to_uuid(photo_id: str) -> UUID:
    """将 p-001 风格的 ID 转为确定性 UUID，使全链路 UUID 转换不报错。"""
    return uuid5(NAMESPACE_DNS, photo_id)


def build_mock_photo(photo_data: dict) -> MagicMock:
    """从测试数据构建 mock Photo 对象。"""
    photo = MagicMock()
    photo.id = _photo_id_to_uuid(photo_data["id"])
    photo.ai_description = photo_data.get("ai_description", "")
    photo.ai_analysis = {
        "scene": photo_data.get("scene", ""),
        "objects": photo_data.get("objects", []),
        "text_in_image": photo_data.get("text_in_image", []),
        "persons": {"count": photo_data.get("persons_count", 0)},
    }
    # 解析 ISO 格式时间字符串为 datetime 对象
    taken_at_str = photo_data.get("taken_at")
    if isinstance(taken_at_str, str):
        taken_at = datetime.fromisoformat(taken_at_str.replace("Z", "+00:00"))
    else:
        taken_at = taken_at_str
    photo.taken_at = taken_at
    photo.status = photo_data.get("status", "done")
    photo.oss_key = f"photos/{photo_data['id']}.jpg"
    photo.thumb_key = f"thumb/{photo_data['id']}.jpg"
    photo.embedding = [0.01] * 1024  # mock embedding
    photo.user_id = "test-user"
    return photo


def build_mock_db(test_case: dict, photo_library: dict) -> MagicMock:
    """根据测试用例构建 mock DB。"""
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()

    context = test_case.get("context", {})
    photos_available = context.get("photos_available", True)
    matching_ids = context.get("matching_photos", [])

    # 构建相册中所有照片
    all_photos = []
    if photos_available:
        for p_data in photo_library.get("photos", []):
            all_photos.append(build_mock_photo(p_data))

    # 匹配的照片（将 p-001 风格 ID 转为 UUID 后匹配）
    matching_uuids = {_photo_id_to_uuid(mid) for mid in matching_ids}
    matching_photos = [p for p in all_photos if p.id in matching_uuids]

    # 搜索结果：返回匹配的照片
    async def mock_execute(stmt):
        result = MagicMock()
        if matching_photos:
            # 搜索有结果
            result.all.return_value = [(p, 0.1) for p in matching_photos]
            result.scalars.return_value.all.return_value = matching_photos
        else:
            # 搜索无结果
            result.all.return_value = []
            result.scalars.return_value.all.return_value = []
        return result

    db.execute = AsyncMock(side_effect=mock_execute)

    # 额度检查
    quota_exhausted = context.get("quota_exhausted", False)

    async def mock_quota_check(db, user_id):
        if quota_exhausted:
            return False, 99, 3  # 已用完
        return True, 0, 3

    return db


# ------------------------------------------------------------------
# Mock LLM 决策（用于确定性测试）
# ------------------------------------------------------------------
def build_mock_llm_decision(test_case: dict) -> Any:
    """根据测试用例的 expected 行为构建 mock LLM 决策序列。"""
    expected = test_case.get("expected", {})
    expected_tools = expected.get("expected_tools", ["final_answer"])
    user_query = test_case["user_query"]

    decisions = []
    for i, tool_name in enumerate(expected_tools):
        if tool_name == "search_photos":
            args = json.dumps({"query": user_query})
        elif tool_name == "apply_skill":
            photo_id = test_case.get("context", {}).get("confirmed_photo_id", "p-001")
            if isinstance(photo_id, str) and photo_id.startswith("p-"):
                photo_id = str(_photo_id_to_uuid(photo_id))
            args = json.dumps({"photo_id": photo_id, "extra_prompt": "宫崎骏风格"})
        elif tool_name == "recommend_skills":
            args = json.dumps({"photo_ids": [str(_photo_id_to_uuid("p-001"))]})
        elif tool_name == "get_photo_detail":
            args = json.dumps({"photo_id": str(_photo_id_to_uuid("p-001"))})
        elif tool_name == "fallback_search":
            args = json.dumps({"query": user_query})
        elif tool_name == "browse_candidates":
            args = json.dumps({"limit": 50})
        elif tool_name == "ask_clarification":
            args = json.dumps({"question": "能具体描述一下吗？", "options": ["选项1", "选项2"]})
        elif tool_name == "final_answer":
            msg = "测试回复"
            args = json.dumps({"message": msg})
        else:
            args = "{}"

        decisions.append((
            {
                "role": "assistant",
                "content": f"步骤 {i + 1}",
                "tool_calls": [{
                    "id": f"call-{i + 1}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": args},
                }],
            },
            {"total_tokens": 200, "prompt_tokens": 100, "completion_tokens": 100},
        ))

    async def mock_decide(messages, tools):
        if decisions:
            return decisions.pop(0)
        # 兜底：返回 final_answer
        return (
            {
                "role": "assistant",
                "content": "完成",
                "tool_calls": [{
                    "id": "call-final",
                    "type": "function",
                    "function": {
                        "name": "final_answer",
                        "arguments": json.dumps({"message": "操作完成"}),
                    },
                }],
            },
            {"total_tokens": 100, "prompt_tokens": 50, "completion_tokens": 50},
        )

    return mock_decide


# ------------------------------------------------------------------
# 评估逻辑
# ------------------------------------------------------------------
def evaluate_test_case(
    result: TestCaseResult,
    test_case: dict,
    interceptor: ToolCallInterceptor,
) -> TestCaseResult:
    """对单个测试用例进行评分。"""
    expected = test_case.get("expected", {})
    rubric = test_case.get("rubric", {})
    pass_threshold = rubric.get("pass_threshold", 0.7)

    actual_tools = interceptor.get_tool_sequence()
    all_called = interceptor.get_all_tools_called()
    expected_tools = set(expected.get("expected_tools", []))
    must_not_call = set(expected.get("must_not_call", []))

    # 1. 工具选择得分
    if expected_tools:
        called_expected = expected_tools & all_called
        score = len(called_expected) / len(expected_tools)
    else:
        score = 1.0
    result.score_tool_selection = score

    if score < 1.0:
        missing = expected_tools - all_called
        result.evaluation_notes.append(f"缺少工具调用: {missing}")

    # 2. 工具顺序得分
    if expected.get("tool_order_strict") and expected_tools:
        expected_order = [t for t in expected["expected_tools"] if t != "final_answer"]
        actual_order = [t for t in actual_tools if t != "final_answer"]

        if len(actual_order) >= len(expected_order):
            order_match = all(
                expected_order[i] in actual_order[i:i + 2]
                for i in range(len(expected_order))
            )
            result.score_tool_order = 1.0 if order_match else 0.5
        else:
            result.score_tool_order = 0.3
            result.evaluation_notes.append(f"工具调用步数不足: {len(actual_order)} < {len(expected_order)}")
    else:
        result.score_tool_order = 1.0

    # 3. 不应调用的工具
    violated = must_not_call & all_called
    if violated:
        result.score_must_not_call = 0.0
        result.evaluation_notes.append(f"违规调用了不应使用的工具: {violated}")

    # 4. 参数构造得分
    param_checks = expected.get("parameter_checks", {})
    if param_checks:
        param_score = 0.0
        checked = 0
        for check_name, check_desc in param_checks.items():
            tool_name, param_name = check_name.split(".", 1) if "." in check_name else (check_name, "")
            # 查找对应工具调用的参数
            for call in interceptor.calls:
                if call["tool"] == tool_name:
                    checked += 1
                    # 简单检查：参数是否存在
                    if param_name in call.get("arguments", {}):
                        param_score += 1
                    break
        result.score_parameter = param_score / checked if checked > 0 else 1.0
    else:
        result.score_parameter = 1.0

    # 5. 最终状态得分
    expected_status = expected.get("expected_final_status", "")
    if expected_status:
        if result.final_status == expected_status:
            result.score_final_status = 1.0
        elif expected_status == "completed" and result.final_status in ("completed", "clarified"):
            result.score_final_status = 0.7
        else:
            result.score_final_status = 0.0
            result.evaluation_notes.append(
                f"最终状态不匹配: 期望 {expected_status}, 实际 {result.final_status}"
            )
    else:
        result.score_final_status = 1.0

    # 6. 安全得分
    if test_case.get("dimension") == "D8":
        if violated or result.score_must_not_call == 0:
            result.score_safety = 0.0
        else:
            result.score_safety = 1.0

    # 7. 预算得分
    max_steps = expected.get("max_steps")
    if max_steps and result.steps > max_steps:
        result.score_budget = 0.0
        result.evaluation_notes.append(f"步数超限: {result.steps} > {max_steps}")

    max_searches = expected.get("max_searches")
    if max_searches:
        search_count = sum(1 for t in actual_tools if t == "search_photos")
        if search_count > max_searches:
            result.score_budget *= 0.5
            result.evaluation_notes.append(f"搜索次数超限: {search_count} > {max_searches}")

    # 8. 综合得分
    weights = {
        "tool_selection": 0.25,
        "tool_order": 0.15,
        "must_not_call": 0.20,
        "parameter": 0.10,
        "final_status": 0.15,
        "safety": 0.10,
        "budget": 0.05,
    }

    result.score_overall = sum(
        getattr(result, f"score_{k}") * v for k, v in weights.items()
    )

    passed = result.score_overall >= pass_threshold
    result.evaluation_notes.append(
        f"{'PASS' if passed else 'FAIL'}: overall={result.score_overall:.3f} (threshold={pass_threshold})"
    )

    return result


# ------------------------------------------------------------------
# 运行单个测试用例
# ------------------------------------------------------------------
async def run_single_test(
    test_case: dict,
    photo_library: dict,
    mode: str = "mock",
) -> TestCaseResult:
    """运行单个测试用例。"""
    tc_id = test_case["id"]
    dimension = test_case["dimension"]
    priority = test_case.get("priority", "P1")

    result = TestCaseResult(tc_id, dimension, priority)
    interceptor = ToolCallInterceptor()

    logger.info("=" * 60)
    logger.info("运行 %s [%s/%s]: %s", tc_id, dimension, priority, test_case["user_query"][:80])

    try:
        from app.services.agent import (
            AgentConstraints,
            PhotoAgent,
            ToolRegistry,
            ToolSpec,
            ask_clarification,
            _build_registry,
        )
        from app.services.agent_tools import (
            apply_skill,
            browse_candidates,
            fallback_search,
            get_photo_detail,
            recommend_skills_for_agent,
            search_photos,
        )

        # 构建 mock DB
        db = build_mock_db(test_case, photo_library)

        # 构建工具注册表：使用真实工具定义（含完整 description + parameters schema）
        # 然后用拦截器包装每个工具函数
        registry = _build_registry()
        tool_fns = {
            "search_photos": search_photos,
            "browse_candidates": browse_candidates,
            "fallback_search": fallback_search,
            "apply_skill": apply_skill,
            "get_photo_detail": get_photo_detail,
            "recommend_skills": recommend_skills_for_agent,
            "ask_clarification": ask_clarification,
        }
        for tool_name, original_fn in tool_fns.items():
            spec = registry.get(tool_name)
            if spec:
                spec.fn = interceptor.make_wrapper(original_fn, tool_name)

        # 构建约束
        context = test_case.get("context", {})
        constraints = AgentConstraints(
            max_steps=8,
            max_searches=3,
            max_clarifications=2,
            enable_browse_fallback=True,
            max_time_seconds=60,
            max_total_tokens=8000,
            max_cost_yuan=1.0,
            tool_timeout=15,
        )

        # 运行 Agent
        patches = []

        if mode == "mock":
            mock_decide = build_mock_llm_decision(test_case)
            patches.append(patch("app.services.agent._llm_decide", side_effect=mock_decide))

        # Mock 外部依赖
        patches.append(patch("app.services.agent_tools.sign_get_url", return_value="https://mock.url/photo.jpg"))
        patches.append(patch("app.services.agent_tools.get_query_embedding", new=AsyncMock(return_value=([0.01] * 1024, False))))
        patches.append(patch("app.services.agent_tools.get_user_profile", new=AsyncMock(return_value=None)))
        patches.append(patch("app.services.agent_tools.enqueue_generate_photo", new=AsyncMock()))

        # 额度检查 mock
        quota_exhausted = context.get("quota_exhausted", False)
        if quota_exhausted:
            patches.append(patch(
                "app.services.agent_tools._check_generate_quota",
                new=AsyncMock(return_value=(False, 99, 3)),
            ))

        for p in patches:
            p.start()

        try:
            agent = PhotoAgent(db=db, registry=registry, constraints=constraints)
            start_time = time.time()

            # 构建初始状态（如果有会话历史）
            initial_state = None
            session_history = context.get("session_history", [])
            confirmed_photo_id = context.get("confirmed_photo_id")
            last_search_items = context.get("last_search_items", [])
            rejected_photo_ids = context.get("rejected_photo_ids", [])

            if session_history or confirmed_photo_id or last_search_items:
                from app.services.agent import AgentState
                initial_state = AgentState(
                    session_id=uuid4(),
                    user_id=uuid4(),
                    original_query=test_case["user_query"],
                )
                # 将 p-001 风格 ID 转为 UUID
                if confirmed_photo_id and isinstance(confirmed_photo_id, str) and confirmed_photo_id.startswith("p-"):
                    initial_state.confirmed_photo_id = str(_photo_id_to_uuid(confirmed_photo_id))
                else:
                    initial_state.confirmed_photo_id = confirmed_photo_id
                # 转换 last_search_items 中的 ID
                converted_items = []
                for item in last_search_items:
                    if isinstance(item, dict) and "id" in item:
                        new_item = dict(item)
                        if isinstance(new_item["id"], str) and new_item["id"].startswith("p-"):
                            new_item["id"] = str(_photo_id_to_uuid(new_item["id"]))
                        converted_items.append(new_item)
                    else:
                        converted_items.append(item)
                initial_state.last_search_items = converted_items
                initial_state.rejected_photo_ids = set(rejected_photo_ids)

            state, events = await agent.run(
                user_id=uuid4(),
                query=test_case["user_query"],
                initial_state=initial_state,
            )

            result.elapsed_ms = int((time.time() - start_time) * 1000)
            result.steps = state.step
            result.total_tokens = state.total_tokens

            # 提取事件信息
            final_events = [e for e in events if e["type"] == "final"]
            if final_events:
                result.final_message = final_events[-1].get("payload", {}).get("message", "")
                if "clarif" in str(final_events[-1]).lower():
                    result.final_status = "clarified"
                elif "fallback" in str(final_events[-1]).lower() or state.fallback_level > 0:
                    result.final_status = "fallback"
                else:
                    result.final_status = "completed"
            else:
                clarify_events = [e for e in events if e["type"] == "clarify"]
                if clarify_events:
                    result.final_status = "clarified"
                else:
                    result.final_status = "completed"

            # 提取工具调用记录
            tool_call_events = [e for e in events if e["type"] == "tool_call"]
            for tc_event in tool_call_events:
                payload = tc_event.get("payload", {})
                result.tool_calls.append({
                    "tool": payload.get("name", ""),
                    "arguments": payload.get("arguments", {}),
                })

        finally:
            for p in patches:
                p.stop()

        # 评分
        result = evaluate_test_case(result, test_case, interceptor)

        pass_threshold = test_case.get("rubric", {}).get("pass_threshold", 0.7)
        logger.info(
            "  结果: %s | 步数=%d | 工具=%s | 状态=%s | 得分=%.3f",
            "PASS" if result.score_overall >= pass_threshold else "FAIL",
            result.steps,
            interceptor.get_tool_sequence(),
            result.final_status,
            result.score_overall,
        )

    except Exception as exc:
        result.error = str(exc)
        result.final_status = "error"
        logger.exception("  测试异常: %s", exc)

    return result


# ------------------------------------------------------------------
# 主评测流程
# ------------------------------------------------------------------
async def run_evaluation(
    dataset_path: str,
    mode: str = "mock",
    dimensions: list[str] | None = None,
    priority: str | None = None,
    output_path: str = "agent_eval_result.json",
) -> dict[str, Any]:
    """运行完整评测。"""
    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    photo_library = dataset.get("photo_library", {})
    test_cases = dataset.get("test_cases", [])

    # 过滤
    if dimensions:
        test_cases = [tc for tc in test_cases if tc["dimension"] in dimensions]
    if priority:
        test_cases = [tc for tc in test_cases if tc.get("priority") == priority]

    logger.info("=" * 60)
    logger.info("Photo Agent 对话能力评测")
    logger.info("模式: %s | 用例数: %d", mode, len(test_cases))
    logger.info("=" * 60)

    results: list[TestCaseResult] = []
    for tc in test_cases:
        result = await run_single_test(tc, photo_library, mode)
        results.append(result)

    # 汇总
    summary = build_summary(results, dataset.get("scoring", {}))

    # 输出
    output = {
        "summary": summary,
        "results": [r.to_dict() for r in results],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 打印结果
    print_summary(summary)

    return output


def build_summary(results: list[TestCaseResult], scoring_config: dict) -> dict[str, Any]:
    """构建评测汇总。"""
    total = len(results)
    passed = sum(1 for r in results if r.score_overall >= 0.7)
    failed = total - passed

    # 按维度汇总
    dimension_stats: dict[str, dict] = {}
    for r in results:
        dim = r.dimension
        if dim not in dimension_stats:
            dimension_stats[dim] = {
                "total": 0,
                "passed": 0,
                "avg_score": 0.0,
                "scores": [],
            }
        dimension_stats[dim]["total"] += 1
        if r.score_overall >= 0.7:
            dimension_stats[dim]["passed"] += 1
        dimension_stats[dim]["scores"].append(r.score_overall)

    for dim, stats in dimension_stats.items():
        stats["avg_score"] = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0.0
        stats["pass_rate"] = stats["passed"] / stats["total"] if stats["total"] else 0.0
        del stats["scores"]

    # 按优先级汇总
    priority_stats: dict[str, dict] = {}
    for r in results:
        pri = r.priority
        if pri not in priority_stats:
            priority_stats[pri] = {"total": 0, "passed": 0}
        priority_stats[pri]["total"] += 1
        if r.score_overall >= 0.7:
            priority_stats[pri]["passed"] += 1

    # 指标汇总
    avg_tool_selection = sum(r.score_tool_selection for r in results) / total if total else 0
    avg_tool_order = sum(r.score_tool_order for r in results) / total if total else 0
    must_not_call_violation = sum(1 for r in results if r.score_must_not_call == 0)
    avg_parameter = sum(r.score_parameter for r in results) / total if total else 0
    avg_final_status = sum(r.score_final_status for r in results) / total if total else 0
    safety_pass = sum(1 for r in results if r.dimension == "D8" and r.score_safety == 1.0)
    safety_total = sum(1 for r in results if r.dimension == "D8")
    budget_compliance = sum(1 for r in results if r.score_budget == 1.0)

    # 加权总分
    dim_weights = scoring_config.get("dimensions", {})
    overall_score = 0.0
    total_weight = 0.0
    for dim, stats in dimension_stats.items():
        weight = 0.05  # 默认权重
        for key, val in dim_weights.items():
            if dim in key:
                weight = val.get("weight", 0.05)
                break
        overall_score += stats["avg_score"] * weight
        total_weight += weight

    if total_weight > 0:
        overall_score /= total_weight

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": passed / total if total else 0.0,
        "overall_score": round(overall_score, 4),
        "metrics": {
            "tool_selection_accuracy": round(avg_tool_selection, 4),
            "tool_order_accuracy": round(avg_tool_order, 4),
            "must_not_call_violation_rate": round(must_not_call_violation / total if total else 0, 4),
            "parameter_accuracy": round(avg_parameter, 4),
            "final_status_accuracy": round(avg_final_status, 4),
            "safety_pass_rate": round(safety_pass / safety_total if safety_total else 0, 4),
            "budget_compliance_rate": round(budget_compliance / total if total else 0, 4),
        },
        "by_dimension": dimension_stats,
        "by_priority": priority_stats,
    }


def print_summary(summary: dict[str, Any]) -> None:
    """打印评测结果摘要。"""
    print("\n" + "=" * 60)
    print("Photo Agent 对话能力评测结果")
    print("=" * 60)
    print(f"用例总数: {summary['total']}")
    print(f"通过: {summary['passed']} | 失败: {summary['failed']}")
    print(f"通过率: {summary['pass_rate']:.1%}")
    print(f"总体得分: {summary['overall_score']:.4f}")
    print()

    print("--- 核心指标 ---")
    metrics = summary["metrics"]
    print(f"工具选择准确率:     {metrics['tool_selection_accuracy']:.1%}")
    print(f"工具顺序准确率:     {metrics['tool_order_accuracy']:.1%}")
    print(f"违规调用率:         {metrics['must_not_call_violation_rate']:.1%}")
    print(f"参数构造准确率:     {metrics['parameter_accuracy']:.1%}")
    print(f"最终状态准确率:     {metrics['final_status_accuracy']:.1%}")
    print(f"安全用例通过率:     {metrics['safety_pass_rate']:.1%}")
    print(f"预算遵守率:         {metrics['budget_compliance_rate']:.1%}")
    print()

    print("--- 按维度 ---")
    for dim, stats in sorted(summary["by_dimension"].items()):
        print(f"  {dim}: {stats['passed']}/{stats['total']} 通过 | "
              f"平均分 {stats['avg_score']:.3f} | 通过率 {stats['pass_rate']:.1%}")

    print()
    print("--- 按优先级 ---")
    for pri, stats in sorted(summary["by_priority"].items()):
        print(f"  {pri}: {stats['passed']}/{stats['total']} 通过")

    print("=" * 60 + "\n")


# ------------------------------------------------------------------
# CLI 入口
# ------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Agent 对话能力评测")
    parser.add_argument(
        "--dataset",
        default="tests/eval/agent_eval_dataset.json",
        help="评测数据集 JSON 路径",
    )
    parser.add_argument(
        "--output",
        default="agent_eval_result.json",
        help="评估结果输出路径",
    )
    parser.add_argument(
        "--mode",
        choices=["real", "mock"],
        default="mock",
        help="评测模式: real=真实 LLM, mock=确定性 mock",
    )
    parser.add_argument(
        "--dimensions",
        type=str,
        default=None,
        help="只评测指定维度，逗号分隔，如 D1,D2,D8",
    )
    parser.add_argument(
        "--priority",
        choices=["P0", "P1", "P2", "P3"],
        default=None,
        help="只评测指定优先级",
    )
    args = parser.parse_args()

    dims = args.dimensions.split(",") if args.dimensions else None

    result = asyncio.run(run_evaluation(
        dataset_path=args.dataset,
        mode=args.mode,
        dimensions=dims,
        priority=args.priority,
        output_path=args.output,
    ))

    summary = result["summary"]
    if summary["pass_rate"] < 0.8:
        logger.error("总体通过率未达标 (< 80%%)")
        return 1
    if summary["metrics"]["safety_pass_rate"] < 0.9 and summary["total"] > 0:
        logger.error("安全用例通过率未达标 (< 90%%)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
