"""Photo Agent 对话评测器。

评测模式：
- replay：按数据集给出的动作序列回放，验证 Agent 循环、工具桩和评分器；
  该模式不衡量模型能力，不能把分数当作模型效果。
- real：调用真实 DashScope 模型完成决策，使用确定性工具桩隔离数据库、OSS
  和异步任务等外部依赖；只有该模式的模型指标可以用于效果报告。

示例：
    python scripts/agent_eval.py --mode replay
    python scripts/agent_eval.py --mode real --output artifacts/agent-eval-real.json
    python scripts/agent_eval.py --mode real --dimensions D1,D2,D8 --priority P0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from unittest.mock import patch
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

# 在导入 app 前提供不访问外部服务的默认配置；真实模式不会伪造 DashScope Key。
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test_secret_for_eval")
os.environ.setdefault("OSS_BUCKET", "photo-agent-dev")
os.environ.setdefault("OSS_KEY_ID", "LTAI_xxx")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

ToolFn = Callable[..., Awaitable[dict[str, Any]]]
CONTROL_TOOLS = {"final_answer"}
INFRA_ERROR_TYPES = {"model_service_error", "model_service_degraded"}


def _json_safe(value: Any) -> Any:
    """把事件和工具结果转换成稳定、可序列化的 JSON 数据。"""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (UUID, date, datetime)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _photo_id_to_uuid(photo_id: str) -> UUID:
    return uuid5(NAMESPACE_DNS, photo_id)


@dataclass
class TestCaseResult:
    test_id: str
    dimension: str
    priority: str
    pass_threshold: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_status: str = ""
    final_message: str = ""
    elapsed_ms: int = 0
    steps: int = 0
    total_tokens: int = 0
    error: str | None = None
    error_type: str | None = None
    score_tool_selection: float = 0.0
    score_tool_order: float = 0.0
    score_must_not_call: float = 1.0
    score_parameter: float = 0.0
    score_final_status: float = 0.0
    score_content: float = 0.0
    score_safety: float = 1.0
    score_budget: float = 1.0
    score_overall: float = 0.0
    evaluation_notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and self.score_overall >= self.pass_threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "dimension": self.dimension,
            "priority": self.priority,
            "passed": self.passed,
            "pass_threshold": self.pass_threshold,
            "tool_calls": _json_safe(self.tool_calls),
            "final_status": self.final_status,
            "final_message": self.final_message[:1000],
            "elapsed_ms": self.elapsed_ms,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "error": self.error,
            "error_type": self.error_type,
            "scores": {
                "tool_selection": round(self.score_tool_selection, 4),
                "tool_order": round(self.score_tool_order, 4),
                "must_not_call": round(self.score_must_not_call, 4),
                "parameter": round(self.score_parameter, 4),
                "final_status": round(self.score_final_status, 4),
                "content": round(self.score_content, 4),
                "safety": round(self.score_safety, 4),
                "budget": round(self.score_budget, 4),
                "overall": round(self.score_overall, 4),
            },
            "notes": self.evaluation_notes,
        }


class ToolCallInterceptor:
    """记录真正进入工具函数的参数和返回结果。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wrap(self, fn: ToolFn, tool_name: str) -> ToolFn:
        async def wrapped(**kwargs: Any) -> dict[str, Any]:
            # user_id/db 是 Agent 运行时注入字段，不属于模型构造的参数。
            public_args = {
                k: v for k, v in kwargs.items() if k not in {"user_id", "db"}
            }
            record: dict[str, Any] = {
                "tool": tool_name,
                "arguments": _json_safe(public_args),
            }
            self.calls.append(record)
            try:
                result = await fn(**kwargs)
            except Exception as exc:
                record["result_ok"] = False
                record["error"] = str(exc)
                raise
            record["result_ok"] = bool(result.get("ok", True))
            record["result"] = _json_safe(result)
            return result

        return wrapped


def _photo_item(photo_id: str, photo_library: dict[str, Any]) -> dict[str, Any]:
    photos = {p["id"]: p for p in photo_library.get("photos", [])}
    source = photos.get(photo_id, {"id": photo_id, "ai_description": "测试照片"})
    return {
        "id": str(_photo_id_to_uuid(photo_id)),
        "source_id": photo_id,
        "ai_description": source.get("ai_description", ""),
        "scene": source.get("scene", ""),
        "objects": source.get("objects", []),
        "taken_at": source.get("taken_at"),
        "status": source.get("status", "done"),
        "thumb_url": f"https://mock.local/{photo_id}.jpg",
    }


def build_tool_stubs(
    test_case: dict[str, Any],
    photo_library: dict[str, Any],
) -> dict[str, ToolFn]:
    """为模型评测构建确定性工具桩，避免 Mock ORM 泄漏到业务结果。"""
    context = test_case.get("context", {})
    photos_available = bool(context.get("photos_available", True))
    matching_ids = list(context.get("matching_photos", [])) if photos_available else []
    similar_ids = list(context.get("similar_photos", [])) if photos_available else []
    all_ids = (
        [p["id"] for p in photo_library.get("photos", [])] if photos_available else []
    )

    async def search_photos(**_: Any) -> dict[str, Any]:
        items = [_photo_item(pid, photo_library) for pid in matching_ids]
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "next_cursor": None,
            "hint": f"找到 {len(items)} 张照片" if items else "没有找到匹配照片",
        }

    async def fallback_search(**_: Any) -> dict[str, Any]:
        ids = similar_ids or matching_ids
        items = [_photo_item(pid, photo_library) for pid in ids]
        return {
            "ok": True,
            "items": items,
            "fallback_level": 2 if items else 3,
            "hint": "兜底找到相似照片" if items else "兜底后仍没有匹配照片",
        }

    async def browse_candidates(limit: int = 50, **_: Any) -> dict[str, Any]:
        items = [_photo_item(pid, photo_library) for pid in all_ids[:limit]]
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "next_cursor": None,
            "hint": f"列出 {len(items)} 张照片" if items else "相册中暂时没有照片",
        }

    async def apply_skill(photo_id: UUID, **_: Any) -> dict[str, Any]:
        if context.get("quota_exhausted"):
            return {
                "ok": False,
                "generation_id": None,
                "status": "quota_exceeded",
                "hint": "今日免费额度已用完",
            }
        return {
            "ok": True,
            "generation_id": str(uuid5(NAMESPACE_DNS, f"generation:{photo_id}")),
            "status": "queued",
            "hint": "已创建照片改造任务",
        }

    async def get_photo_detail(photo_id: UUID, **_: Any) -> dict[str, Any]:
        source_id = next(
            (pid for pid in all_ids if _photo_id_to_uuid(pid) == photo_id),
            None,
        )
        if source_id is None:
            return {"ok": False, "data": None, "hint": "照片不存在或无权访问"}
        return {
            "ok": True,
            "data": _photo_item(source_id, photo_library),
            "hint": "已获取照片详情",
        }

    async def recommend_skills(**_: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "items": [
                {"id": "skill-watercolor", "name": "水彩画"},
                {"id": "skill-anime", "name": "动漫风"},
            ],
            "hint": "为你推荐 2 个 Skill",
        }

    async def ask_clarification(
        question: str,
        options: list[str] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "needs_clarification": True,
            "question": question,
            "options": options or [],
        }

    return {
        "search_photos": search_photos,
        "fallback_search": fallback_search,
        "browse_candidates": browse_candidates,
        "apply_skill": apply_skill,
        "get_photo_detail": get_photo_detail,
        "recommend_skills": recommend_skills,
        "ask_clarification": ask_clarification,
    }


def build_real_photo_library(manifest_path: str) -> dict[str, Any]:
    """把人工复核图片清单转换为 Agent 工具桩需要的相册结构。

    这让真实 LLM 的工具选择评测能使用本批图片的真实内容标注，同时继续隔离
    PostgreSQL、OSS 和异步任务；它衡量的是 Agent 决策，不是向量检索质量。
    """
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    photos = []
    for image in payload.get("images", []):
        truth = image["ground_truth"]
        photos.append(
            {
                "id": image["id"],
                "ai_description": truth["summary"],
                "scene": truth["acceptable_scenes"][0],
                "objects": truth["required_objects"],
                "tags": truth["search_terms"],
                "text_in_image": truth["required_text"],
                "persons_count": truth["persons"]["min"],
                "status": "done",
            }
        )
    return {"description": payload.get("name", "人工复核图片库"), "photos": photos}


def _rule_exact_value(rule: Any) -> Any:
    if isinstance(rule, dict):
        if "equals" in rule:
            return rule["equals"]
        if "equals_photo_id" in rule:
            return str(_photo_id_to_uuid(rule["equals_photo_id"]))
    return rule if not isinstance(rule, dict) else None


def _replay_arguments(test_case: dict[str, Any], tool_name: str) -> dict[str, Any]:
    expected = test_case.get("expected", {})
    context = test_case.get("context", {})
    checks = expected.get("parameter_checks", {})

    if tool_name in {"search_photos", "fallback_search"}:
        args: dict[str, Any] = {"query": test_case["user_query"]}
        for key, rule in checks.items():
            prefix = f"{tool_name}."
            if key.startswith(prefix):
                value = _rule_exact_value(rule)
                if value is not None:
                    args[key[len(prefix) :]] = value
                elif key.endswith(".query") and isinstance(rule, dict):
                    required = rule.get("contains_all") or rule.get("contains_any")
                    if required:
                        args["query"] = " ".join(str(token) for token in required)
        return args
    if tool_name == "apply_skill":
        photo_id = context.get("confirmed_photo_id") or "p-001"
        args = {
            "photo_id": str(_photo_id_to_uuid(photo_id))
            if str(photo_id).startswith("p-")
            else str(photo_id),
            "extra_prompt": "宫崎骏动漫风格",
        }
        return args
    if tool_name == "get_photo_detail":
        photo_id = context.get("confirmed_photo_id") or "p-001"
        return {
            "photo_id": str(_photo_id_to_uuid(photo_id))
            if str(photo_id).startswith("p-")
            else str(photo_id)
        }
    if tool_name == "recommend_skills":
        photo_id = context.get("confirmed_photo_id") or "p-001"
        return {"photo_ids": [str(_photo_id_to_uuid(photo_id))]}
    if tool_name == "browse_candidates":
        return {"limit": 50}
    if tool_name == "ask_clarification":
        hint = expected.get("expected_hint_contains") or "请补充照片线索"
        return {
            "question": f"{hint}：能再具体说明一下吗？",
            "options": ["按时间", "按地点", "按类型"],
        }
    if tool_name == "final_answer":
        parts = list(expected.get("expected_result_contains", []))
        if expected.get("expected_hint_contains"):
            parts.append(expected["expected_hint_contains"])
        return {"message": "，".join(parts) or "操作已完成"}
    return {}


def build_replay_llm_decision(
    test_case: dict[str, Any],
) -> Callable[..., Awaitable[tuple[dict, dict]]]:
    """按标注动作回放；只用于评测管线自检，不代表模型推理结果。"""
    expected = test_case.get("expected", {})
    sequence = list(expected.get("expected_tools", []))
    min_calls = expected.get("min_tool_calls", {})
    for tool_name, minimum in min_calls.items():
        present = sequence.count(tool_name)
        if minimum > present:
            insertion = sequence.index(tool_name) + 1 if tool_name in sequence else 0
            sequence[insertion:insertion] = [tool_name] * (minimum - present)
    if not sequence:
        sequence = ["final_answer"]

    decisions: list[tuple[dict[str, Any], dict[str, int]]] = []
    for index, tool_name in enumerate(sequence, start=1):
        arguments = _replay_arguments(test_case, tool_name)
        decisions.append(
            (
                {
                    "role": "assistant",
                    "content": f"回放步骤 {index}",
                    "tool_calls": [
                        {
                            "id": f"replay-{index}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
            )
        )

    async def replay_decide(_: list[dict], __: list[dict]) -> tuple[dict, dict]:
        if decisions:
            return decisions.pop(0)
        return (
            {
                "role": "assistant",
                "content": "回放完成",
                "tool_calls": [
                    {
                        "id": "replay-final",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps(
                                {"message": "回放完成"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            },
            {"total_tokens": 0},
        )

    return replay_decide


def _parameter_matches(actual: Any, rule: Any) -> bool:
    """执行结构化参数断言；不再把自然语言描述当成已验证规则。"""
    actual_text = str(actual)
    if not isinstance(rule, dict):
        expected = (
            str(_photo_id_to_uuid(rule)) if str(rule).startswith("p-") else str(rule)
        )
        return actual_text == expected

    if rule.get("not_empty") and actual in (None, "", [], {}):
        return False
    if "equals" in rule and actual_text != str(rule["equals"]):
        return False
    if "equals_photo_id" in rule:
        if actual_text != str(_photo_id_to_uuid(rule["equals_photo_id"])):
            return False
    lowered = actual_text.lower()
    contains_all = [str(v).lower() for v in rule.get("contains_all", [])]
    contains_any = [str(v).lower() for v in rule.get("contains_any", [])]
    excludes = [str(v).lower() for v in rule.get("excludes", [])]
    if contains_all and not all(token in lowered for token in contains_all):
        return False
    if contains_any and not any(token in lowered for token in contains_any):
        return False
    if excludes and any(token in lowered for token in excludes):
        return False
    return True


def _selection_score(expected: list[str], actual: list[str]) -> float:
    expected_counts = Counter(t for t in expected if t not in CONTROL_TOOLS)
    actual_counts = Counter(t for t in actual if t not in CONTROL_TOOLS)
    if not expected_counts:
        return 1.0 if not actual_counts else 0.0
    true_positive = sum(
        min(count, actual_counts.get(tool_name, 0))
        for tool_name, count in expected_counts.items()
    )
    precision = true_positive / sum(actual_counts.values()) if actual_counts else 0.0
    recall = true_positive / sum(expected_counts.values())
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def evaluate_test_case(
    result: TestCaseResult, test_case: dict[str, Any]
) -> TestCaseResult:
    expected = test_case.get("expected", {})
    expected_tools = list(expected.get("expected_tools", []))
    for tool_name, minimum in expected.get("min_tool_calls", {}).items():
        if minimum > expected_tools.count(tool_name):
            insertion = (
                expected_tools.index(tool_name) + 1
                if tool_name in expected_tools
                else 0
            )
            expected_tools[insertion:insertion] = [tool_name] * (
                minimum - expected_tools.count(tool_name)
            )
    actual_tools = [call["tool"] for call in result.tool_calls]
    expected_tool_sets = [expected_tools]
    expected_tool_sets.extend(
        list(candidate) for candidate in expected.get("expected_tools_any_of", [])
    )
    selection_scores = [
        _selection_score(candidate, actual_tools) for candidate in expected_tool_sets
    ]
    best_tool_index = max(
        range(len(selection_scores)), key=selection_scores.__getitem__
    )
    expected_tools = expected_tool_sets[best_tool_index]
    result.score_tool_selection = selection_scores[best_tool_index]
    if result.score_tool_selection < 1:
        result.evaluation_notes.append(
            f"工具集合不匹配: expected_any_of={expected_tool_sets}, actual={actual_tools}"
        )

    expected_order = [t for t in expected_tools if t not in CONTROL_TOOLS]
    actual_order = [t for t in actual_tools if t not in CONTROL_TOOLS]
    if expected.get("tool_order_strict"):
        result.score_tool_order = 1.0 if actual_order == expected_order else 0.0
        if result.score_tool_order == 0:
            result.evaluation_notes.append(
                f"工具顺序不匹配: expected={expected_order}, actual={actual_order}"
            )
    else:
        result.score_tool_order = 1.0

    must_not_call = set(expected.get("must_not_call", []))
    violated = must_not_call.intersection(actual_tools)
    if violated:
        result.score_must_not_call = 0.0
        result.evaluation_notes.append(f"违规调用工具: {sorted(violated)}")

    checks = expected.get("parameter_checks", {})
    if checks:
        passed_checks = 0
        for check_name, rule in checks.items():
            tool_name, parameter_name = check_name.split(".", 1)
            call = next((c for c in result.tool_calls if c["tool"] == tool_name), None)
            actual = call.get("arguments", {}).get(parameter_name) if call else None
            if (
                call
                and parameter_name in call.get("arguments", {})
                and _parameter_matches(actual, rule)
            ):
                passed_checks += 1
            else:
                result.evaluation_notes.append(
                    f"参数断言失败: {check_name}, expected={rule!r}, actual={actual!r}"
                )
        result.score_parameter = passed_checks / len(checks)
    else:
        result.score_parameter = 1.0

    expected_status = expected.get("expected_final_status")
    allowed_statuses = list(expected.get("expected_final_status_any_of", []))
    if not allowed_statuses and expected_status:
        allowed_statuses = [expected_status]
    result.score_final_status = (
        1.0 if not allowed_statuses or result.final_status in allowed_statuses else 0.0
    )
    if result.score_final_status == 0:
        result.evaluation_notes.append(
            f"最终状态不匹配: expected_any_of={allowed_statuses}, actual={result.final_status}"
        )

    content_checks = 0
    content_passed = 0
    tool_result_text = json.dumps(
        [call.get("result") for call in result.tool_calls],
        ensure_ascii=False,
        default=str,
    )
    combined_text = f"{result.final_message}\n{tool_result_text}"
    for token in expected.get("expected_result_contains", []):
        content_checks += 1
        if str(token) in combined_text:
            content_passed += 1
        else:
            result.evaluation_notes.append(f"结果缺少关键内容: {token}")
    contains_any = [str(token) for token in expected.get("expected_result_contains_any", [])]
    if contains_any:
        content_checks += 1
        if any(token in combined_text for token in contains_any):
            content_passed += 1
        else:
            result.evaluation_notes.append(
                f"结果未包含任一允许内容: {contains_any}"
            )
    hint = expected.get("expected_hint_contains")
    if hint:
        content_checks += 1
        if str(hint) in result.final_message:
            content_passed += 1
        else:
            result.evaluation_notes.append(f"最终回复缺少提示: {hint}")
    for token in expected.get("forbidden_final_contains", []):
        content_checks += 1
        if str(token) not in result.final_message:
            content_passed += 1
        else:
            result.evaluation_notes.append(f"最终回复包含禁用内容: {token}")
    expected_error_type = expected.get("expected_error_type")
    if expected_error_type:
        content_checks += 1
        if result.error_type == expected_error_type:
            content_passed += 1
        else:
            result.evaluation_notes.append(
                f"错误类型不匹配: expected={expected_error_type}, actual={result.error_type}"
            )
    result.score_content = content_passed / content_checks if content_checks else 1.0

    if test_case.get("dimension") == "D8":
        result.score_safety = (
            1.0
            if result.score_must_not_call == 1.0 and result.score_content == 1.0
            else 0.0
        )

    max_steps = expected.get("max_steps")
    if max_steps is not None and result.steps > max_steps:
        result.score_budget = 0.0
        result.evaluation_notes.append(f"步数超限: {result.steps} > {max_steps}")
    max_searches = expected.get("max_searches")
    search_count = actual_tools.count("search_photos")
    if max_searches is not None and search_count > max_searches:
        result.score_budget = 0.0
        result.evaluation_notes.append(f"搜索次数超限: {search_count} > {max_searches}")
    max_time = expected.get("max_time_seconds")
    if max_time is not None and result.elapsed_ms > float(max_time) * 1000:
        result.score_budget = 0.0
        result.evaluation_notes.append(
            f"耗时超限: {result.elapsed_ms}ms > {float(max_time) * 1000:.0f}ms"
        )
    max_total_tokens = expected.get("max_total_tokens")
    if max_total_tokens is not None and result.total_tokens > int(max_total_tokens):
        result.score_budget = 0.0
        result.evaluation_notes.append(
            f"Token 超限: {result.total_tokens} > {int(max_total_tokens)}"
        )

    weights = {
        "tool_selection": 0.20,
        "tool_order": 0.10,
        "must_not_call": 0.20,
        "parameter": 0.10,
        "final_status": 0.15,
        "content": 0.15,
        "safety": 0.05,
        "budget": 0.05,
    }
    result.score_overall = sum(
        getattr(result, f"score_{name}") * weight for name, weight in weights.items()
    )
    if result.error:
        result.score_overall = 0.0
    result.evaluation_notes.append(
        f"{'PASS' if result.passed else 'FAIL'}: "
        f"overall={result.score_overall:.3f}, threshold={result.pass_threshold:.3f}"
    )
    return result


def _build_initial_state(
    test_case: dict[str, Any],
    user_id: UUID,
) -> Any | None:
    context = test_case.get("context", {})
    if not any(
        context.get(key)
        for key in (
            "session_history",
            "confirmed_photo_id",
            "last_search_items",
            "rejected_photo_ids",
        )
    ):
        return None
    from app.services.agent import AgentState

    state = AgentState(
        session_id=uuid4(),
        user_id=user_id,
        original_query=test_case["user_query"],
    )
    confirmed = context.get("confirmed_photo_id")
    if confirmed:
        state.confirmed_photo_id = (
            str(_photo_id_to_uuid(confirmed))
            if str(confirmed).startswith("p-")
            else str(confirmed)
        )
    state.last_search_items = []
    for raw_item in context.get("last_search_items", []):
        item = dict(raw_item) if isinstance(raw_item, dict) else {"id": raw_item}
        item_id = item.get("id")
        if str(item_id).startswith("p-"):
            item["id"] = str(_photo_id_to_uuid(str(item_id)))
        state.last_search_items.append(item)
    state.rejected_photo_ids = {
        str(_photo_id_to_uuid(pid)) if str(pid).startswith("p-") else str(pid)
        for pid in context.get("rejected_photo_ids", [])
    }
    # 当前 AgentState 只保存结构化步骤历史；自然语言历史作为审计信息保留。
    if context.get("session_history"):
        state.history.append({"session_history": context["session_history"]})
    return state


def _collect_result_from_events(
    result: TestCaseResult,
    events: list[dict[str, Any]],
    state: Any,
    interceptor: ToolCallInterceptor,
) -> None:
    result.tool_calls = list(interceptor.calls)
    for event in events:
        if event.get("type") != "tool_call":
            continue
        payload = event.get("payload", {})
        tool_name = payload.get("tool", "")
        if tool_name != "final_answer":
            continue
        raw_arguments = payload.get("arguments", {})
        try:
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else raw_arguments
            )
        except json.JSONDecodeError:
            arguments = {"message": str(raw_arguments)}
        result.tool_calls.append(
            {
                "tool": "final_answer",
                "arguments": _json_safe(arguments),
                "result_ok": True,
                "result": {"ok": True, "message": arguments.get("message", "")},
            }
        )

    think_events = [event for event in events if event.get("type") == "think"]
    result.steps = len(think_events)
    result.total_tokens = state.total_tokens
    clarify_events = [event for event in events if event.get("type") == "clarify"]
    final_events = [event for event in events if event.get("type") == "final"]
    error_events = [event for event in events if event.get("type") == "error"]
    degraded_events = [
        event
        for event in final_events
        if event.get("payload", {}).get("fallback") == "browse_candidates"
    ]
    if degraded_events:
        result.final_status = "error"
        result.error_type = "model_service_degraded"
        result.final_message = str(
            degraded_events[-1].get("payload", {}).get("message", "")
        )
        result.error = result.final_message or "Agent LLM circuit breaker is open"
    elif clarify_events:
        result.final_status = "clarified"
        result.final_message = str(
            clarify_events[-1].get("payload", {}).get("question", "")
        )
    elif error_events and not final_events:
        payload = error_events[-1].get("payload", {})
        result.final_status = "error"
        result.error_type = "model_service_error"
        result.final_message = str(payload.get("message", ""))
        source_type = payload.get("error_type", "unknown")
        result.error = f"{source_type}: {result.final_message}"
    elif state.fallback_level > 0 or any(
        call["tool"] == "fallback_search" for call in result.tool_calls
    ):
        result.final_status = "fallback"
        if final_events:
            result.final_message = str(
                final_events[-1].get("payload", {}).get("message", "")
            )
    else:
        result.final_status = "completed"
        if final_events:
            result.final_message = str(
                final_events[-1].get("payload", {}).get("message", "")
            )


def _run_request_validation_case(
    result: TestCaseResult, test_case: dict[str, Any]
) -> bool:
    expected = test_case.get("expected", {})
    if expected.get("expected_error_type") != "validation":
        return False
    from pydantic import ValidationError

    from app.schemas.agent import AgentRunRequest

    query = test_case["user_query"]
    target_length = test_case.get("context", {}).get("validation_query_length")
    if target_length and len(query) < int(target_length):
        query = query.ljust(int(target_length), "很")
    try:
        AgentRunRequest(query=query)
    except ValidationError as exc:
        result.final_status = "error"
        result.error_type = "validation"
        result.final_message = str(exc)
    else:
        result.final_status = "completed"
        result.final_message = "请求层未拒绝该输入"
    return True


async def run_single_test(
    test_case: dict[str, Any],
    photo_library: dict[str, Any],
    mode: str,
) -> TestCaseResult:
    threshold = float(test_case.get("rubric", {}).get("pass_threshold", 0.7))
    result = TestCaseResult(
        test_id=test_case["id"],
        dimension=test_case["dimension"],
        priority=test_case.get("priority", "P1"),
        pass_threshold=threshold,
    )
    logger.info(
        "运行 %s [%s/%s]: %s",
        result.test_id,
        result.dimension,
        result.priority,
        test_case["user_query"][:80],
    )

    started = time.monotonic()
    try:
        if _run_request_validation_case(result, test_case):
            return evaluate_test_case(result, test_case)

        from app.services.agent import AgentConstraints, PhotoAgent, _build_registry
        from app.services.circuit_breaker import agent_llm_breaker

        if mode == "real":
            # 每条评测用例应相互独立，不能让前一条的熔断状态污染后续分数。
            agent_llm_breaker.reset()

        interceptor = ToolCallInterceptor()
        stubs = build_tool_stubs(test_case, photo_library)
        registry = _build_registry()
        wrapped_stubs: dict[str, ToolFn] = {}
        for tool_name, stub in stubs.items():
            wrapped = interceptor.wrap(stub, tool_name)
            wrapped_stubs[tool_name] = wrapped
            spec = registry.get(tool_name)
            if spec is not None:
                spec.fn = wrapped

        context = test_case.get("context", {})
        default_constraints = AgentConstraints()
        constraints = AgentConstraints(
            max_steps=int(context.get("max_steps", default_constraints.max_steps)),
            max_searches=int(
                context.get("max_searches", default_constraints.max_searches)
            ),
            max_clarifications=int(
                context.get(
                    "max_clarifications", default_constraints.max_clarifications
                )
            ),
            enable_browse_fallback=True,
            max_time_seconds=int(
                context.get("max_time_seconds", default_constraints.max_time_seconds)
            ),
            max_total_tokens=int(
                context.get("max_total_tokens", default_constraints.max_total_tokens)
            ),
            max_cost_yuan=float(
                context.get("max_cost_yuan", default_constraints.max_cost_yuan)
            ),
            tool_timeout=min(default_constraints.tool_timeout, 5),
        )
        user_id = uuid4()
        agent = PhotoAgent(db=object(), registry=registry, constraints=constraints)
        initial_state = _build_initial_state(test_case, user_id)

        patches = [
            patch(
                "app.services.agent.fallback_search",
                new=wrapped_stubs["fallback_search"],
            ),
            patch(
                "app.services.agent.browse_candidates",
                new=wrapped_stubs["browse_candidates"],
            ),
        ]

        if mode == "replay":
            patches.append(
                patch(
                    "app.services.agent._llm_decide",
                    new=build_replay_llm_decision(test_case),
                )
            )
        for active_patch in patches:
            active_patch.start()
        try:
            state, events = await agent.run(
                user_id=user_id,
                query=test_case["user_query"],
                initial_state=initial_state,
            )
        finally:
            for active_patch in reversed(patches):
                active_patch.stop()

        _collect_result_from_events(result, events, state, interceptor)
    except Exception as exc:
        result.error = str(exc)
        result.error_type = "runner_error"
        result.final_status = "error"
        logger.exception("评测用例 %s 运行失败", result.test_id)
    finally:
        result.elapsed_ms = int((time.monotonic() - started) * 1000)

    return evaluate_test_case(result, test_case)


def _dimension_config(scoring_config: dict[str, Any], dimension: str) -> dict[str, Any]:
    for key, config in scoring_config.get("dimensions", {}).items():
        if key == dimension or key.startswith(f"{dimension}_"):
            return config
    return {"weight": 1.0, "pass_threshold": 0.7}


def build_summary(
    results: list[TestCaseResult],
    scoring_config: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    total = len(results)
    passed = sum(result.passed for result in results)
    errors = sum(result.error is not None for result in results)
    by_dimension: dict[str, dict[str, Any]] = {}
    by_priority: dict[str, dict[str, Any]] = {}

    for result in results:
        dim = by_dimension.setdefault(
            result.dimension,
            {"total": 0, "passed": 0, "scores": []},
        )
        dim["total"] += 1
        dim["passed"] += int(result.passed)
        dim["scores"].append(result.score_overall)
        priority = by_priority.setdefault(result.priority, {"total": 0, "passed": 0})
        priority["total"] += 1
        priority["passed"] += int(result.passed)

    weighted_score = 0.0
    total_weight = 0.0
    for dimension, stats in by_dimension.items():
        stats["avg_score"] = sum(stats.pop("scores")) / stats["total"]
        stats["pass_rate"] = stats["passed"] / stats["total"]
        config = _dimension_config(scoring_config, dimension)
        stats["threshold"] = float(config.get("pass_threshold", 0.7))
        stats["meets_threshold"] = stats["avg_score"] >= stats["threshold"]
        weight = float(config.get("weight", 1.0))
        weighted_score += stats["avg_score"] * weight
        total_weight += weight
    overall_score = weighted_score / total_weight if total_weight else 0.0
    overall_threshold = float(scoring_config.get("overall_pass_threshold", 0.8))

    def average(attribute: str) -> float:
        return (
            sum(float(getattr(result, attribute)) for result in results) / total
            if total
            else 0.0
        )

    safety_results = [result for result in results if result.dimension == "D8"]
    safety_pass_rate = (
        sum(result.score_safety == 1.0 for result in safety_results)
        / len(safety_results)
        if safety_results
        else 1.0
    )
    metrics_valid = mode == "real"
    gate_passed = (
        errors == 0 and passed == total
        if mode == "replay"
        else errors == 0
        and overall_score >= overall_threshold
        and safety_pass_rate
        >= _dimension_config(scoring_config, "D8").get("pass_threshold", 0.9)
    )
    return {
        "mode": mode,
        "model_metrics_valid": metrics_valid,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "errors": errors,
        "pass_rate": passed / total if total else 0.0,
        "overall_score": round(overall_score, 4),
        "overall_threshold": overall_threshold,
        "overall_passed": overall_score >= overall_threshold,
        "gate_passed": bool(gate_passed),
        "metrics": {
            "tool_selection_accuracy": round(average("score_tool_selection"), 4),
            "tool_order_accuracy": round(average("score_tool_order"), 4),
            "must_not_call_violation_rate": round(
                sum(result.score_must_not_call == 0 for result in results) / total
                if total
                else 0,
                4,
            ),
            "parameter_accuracy": round(average("score_parameter"), 4),
            "final_status_accuracy": round(average("score_final_status"), 4),
            "content_accuracy": round(average("score_content"), 4),
            "safety_pass_rate": round(safety_pass_rate, 4),
            "budget_compliance_rate": round(
                sum(result.score_budget == 1.0 for result in results) / total
                if total
                else 0,
                4,
            ),
        },
        "by_dimension": by_dimension,
        "by_priority": by_priority,
    }


def validate_dataset(dataset: dict[str, Any]) -> None:
    test_cases = dataset.get("test_cases", [])
    ids = [test_case.get("id") for test_case in test_cases]
    duplicates = sorted(test_id for test_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"数据集存在重复 ID: {duplicates}")
    for test_case in test_cases:
        if not test_case.get("id") or not test_case.get("dimension"):
            raise ValueError("每条用例必须包含 id 和 dimension")
        expected = test_case.get("expected", {})
        expected_tools = list(expected.get("expected_tools", []))
        if "fallback_search" in expected_tools:
            minimum_searches = int(
                expected.get("min_tool_calls", {}).get("search_photos", 0)
            )
            if minimum_searches < 2:
                raise ValueError(
                    f"{test_case['id']} 的 fallback_search 必须在两次 "
                    "search_photos 失败后执行"
                )
        if "ask_clarification" in expected_tools:
            alternative_tools = expected.get("expected_tools_any_of", [])
            allowed_statuses = set(expected.get("expected_final_status_any_of", []))
            if [] not in alternative_tools or not {"clarified", "completed"}.issubset(
                allowed_statuses
            ):
                raise ValueError(
                    f"{test_case['id']} 必须同时接受结构化澄清和有效自然语言澄清"
                )
        for name, rule in (
            expected.get("parameter_checks", {}).items()
        ):
            if "." not in name:
                raise ValueError(f"参数断言必须使用 tool.parameter 格式: {name}")
            if isinstance(rule, dict):
                supported = {
                    "equals",
                    "equals_photo_id",
                    "not_empty",
                    "contains_all",
                    "contains_any",
                    "excludes",
                }
                unknown = set(rule) - supported
                if unknown:
                    raise ValueError(
                        f"{test_case['id']} 使用未知参数断言: {sorted(unknown)}"
                    )


def validate_real_mode() -> None:
    from app.services.agent import _is_mock_llm

    if _is_mock_llm():
        raise ValueError(
            "real 模式需要有效的 DASHSCOPE_API_KEY；当前为空或仍是占位值，已拒绝静默降级。"
        )


async def validate_real_connectivity() -> None:
    """用最小请求验证真实模型连通性，失败时不产生误导性的能力分数。"""
    from app.config import settings
    from app.services.agent import _llm_decide
    from app.services.circuit_breaker import agent_llm_breaker

    agent_llm_breaker.reset()
    final_answer_schema = {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": "Return a short final answer.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    }
    try:
        await _llm_decide(
            [
                {"role": "system", "content": "You are a connectivity probe."},
                {"role": "user", "content": "Reply briefly."},
            ],
            [final_answer_schema],
        )
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        workspace_hint = (
            " 当前使用子工作区 Key，请同时确认 qwen-plus 调用权限和地域对应的专属 Endpoint。"
            if settings.dashscope_api_key.startswith("sk-ws-")
            else ""
        )
        raise ValueError(
            "DashScope 连通性预检失败，未开始能力评测："
            f"{type(exc).__name__}: {detail}.{workspace_hint} "
            "请先检查网络、DASHSCOPE_CHAT_URL、Key 权限和模型名。"
        ) from exc
    finally:
        agent_llm_breaker.reset()


async def run_evaluation(
    dataset_path: str,
    mode: str = "replay",
    dimensions: list[str] | None = None,
    priority: str | None = None,
    output_path: str = "agent_eval_result.json",
    preflight: bool = True,
    max_infra_errors: int = 3,
    photo_manifest_path: str | None = None,
) -> dict[str, Any]:
    normalized_mode = "replay" if mode == "mock" else mode
    if mode == "mock":
        logger.warning("--mode mock 已弃用，按 replay 模式执行；该模式不衡量模型能力。")
    dataset_file = Path(dataset_path)
    dataset = json.loads(dataset_file.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    if normalized_mode == "real":
        validate_real_mode()
        if preflight:
            await validate_real_connectivity()

    test_cases = list(dataset.get("test_cases", []))
    photo_library = (
        build_real_photo_library(photo_manifest_path)
        if photo_manifest_path
        else dataset.get("photo_library", {})
    )
    if dimensions:
        test_cases = [case for case in test_cases if case["dimension"] in dimensions]
    if priority:
        test_cases = [case for case in test_cases if case.get("priority") == priority]
    logger.info(
        "Photo Agent 评测 | mode=%s | cases=%d", normalized_mode, len(test_cases)
    )

    requested_total = len(test_cases)
    results: list[TestCaseResult] = []
    infra_errors = 0
    aborted = False
    for case in test_cases:
        result = await run_single_test(
            case,
            photo_library,
            normalized_mode,
        )
        results.append(result)
        if result.error_type in INFRA_ERROR_TYPES:
            infra_errors += 1
            if (
                normalized_mode == "real"
                and max_infra_errors > 0
                and infra_errors >= max_infra_errors
            ):
                aborted = True
                logger.error(
                    "连续基础设施错误达到上限（%d），提前终止真实评测",
                    max_infra_errors,
                )
                break
    summary = build_summary(results, dataset.get("scoring", {}), normalized_mode)
    summary["requested_total"] = requested_total
    summary["aborted"] = aborted
    summary["infra_errors"] = infra_errors
    if aborted:
        summary["gate_passed"] = False
    output = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset": str(dataset_file),
            "dataset_version": dataset.get("version"),
            "dataset_role": dataset.get("dataset_role", "unspecified"),
            "mode": normalized_mode,
            "photo_manifest": photo_manifest_path,
        },
        "summary": summary,
        "results": [result.to_dict() for result in results],
    }
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_summary(summary)
    return output


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 68)
    print(f"Photo Agent 评测结果 | mode={summary['mode']}")
    if not summary["model_metrics_valid"]:
        print("注意：replay 仅验证评测管线，以下分数不是模型能力指标。")
    print("=" * 68)
    if summary.get("aborted"):
        print(
            f"基础设施错误达到上限，已提前终止："
            f"完成 {summary['total']}/{summary['requested_total']} 条。"
        )
    print(
        f"用例 {summary['total']} | 通过 {summary['passed']} | "
        f"失败 {summary['failed']} | 运行错误 {summary['errors']}"
    )
    print(
        f"通过率 {summary['pass_rate']:.1%} | 加权分 {summary['overall_score']:.4f} | "
        f"门禁 {'PASS' if summary['gate_passed'] else 'FAIL'}"
    )
    metrics = summary["metrics"]
    print(
        "工具选择 {tool_selection_accuracy:.1%} | 参数 {parameter_accuracy:.1%} | "
        "状态 {final_status_accuracy:.1%} | 内容 {content_accuracy:.1%} | "
        "安全 {safety_pass_rate:.1%}".format(**metrics)
    )
    print("-" * 68)
    for dimension, stats in sorted(summary["by_dimension"].items()):
        print(
            f"{dimension}: {stats['passed']}/{stats['total']} | "
            f"avg={stats['avg_score']:.3f} | threshold={stats['threshold']:.2f} | "
            f"{'PASS' if stats['meets_threshold'] else 'FAIL'}"
        )
    print("=" * 68 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Photo Agent 对话评测器")
    parser.add_argument(
        "--dataset",
        default="tests/eval/agent_eval_dataset.json",
        help="评测数据集 JSON 路径",
    )
    parser.add_argument(
        "--output",
        default="agent_eval_result.json",
        help="结果 JSON 路径",
    )
    parser.add_argument(
        "--photo-manifest",
        help="可选：用人工复核 photo_manifest 替换 Agent 评测中的模拟相册内容",
    )
    parser.add_argument(
        "--mode",
        choices=["real", "replay", "mock"],
        default="replay",
        help="real=真实模型评测；replay=标注动作回放；mock 为 replay 的兼容别名",
    )
    parser.add_argument("--dimensions", help="维度列表，如 D1,D2,D8")
    parser.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过真实模型连通性预检（不推荐）",
    )
    parser.add_argument(
        "--max-infra-errors",
        type=int,
        default=3,
        help="真实评测最多容忍的模型服务错误数，0 表示不提前终止",
    )
    args = parser.parse_args()
    dimensions = args.dimensions.split(",") if args.dimensions else None
    try:
        output = asyncio.run(
            run_evaluation(
                dataset_path=args.dataset,
                mode=args.mode,
                dimensions=dimensions,
                priority=args.priority,
                output_path=args.output,
                preflight=not args.skip_preflight,
                max_infra_errors=args.max_infra_errors,
                photo_manifest_path=args.photo_manifest,
            )
        )
    except ValueError as exc:
        logger.error("评测配置错误: %s", exc)
        return 2
    return 0 if output["summary"]["gate_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
