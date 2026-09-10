"""Agent 可调用的 Tool 封装层。

设计原则：
- 每个 Tool 都是纯函数/协程，签名清晰，返回值可 JSON 序列化；
- Tool 内部处理权限、异常和兜底，不让 Agent 核心关心业务细节；
- Tool 返回统一 dict，包含 ok / data / error / hint 字段，方便 Agent 做下一步决策。
"""

from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telemetry import traced_async
from app.services.search_engine import serialize_photo
from app.models.photo import Photo
from app.services.generation_service import (
    GenerationDomainError,
    confirm_generation,
    generation_confirmation_payload,
    prepare_generation,
)
from app.services.recommend import recommend_skills

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# P1-4: 错误分类系统
# 让 Agent 能根据 error_type 做不同处理：重试/换路径/提示用户/放弃
# ------------------------------------------------------------------
class ToolError(Exception):
    """工具错误基类，携带 error_type 供 Agent 决策。"""

    def __init__(self, message: str, error_type: str = "unknown") -> None:
        self.error_type = error_type
        super().__init__(message)


class RetryableError(ToolError):
    """可重试错误：DB 连接超时、网络抖动等临时故障。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "retryable")


class UserFixableError(ToolError):
    """用户可修复错误：参数无效、权限不足、额度不足等。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "user_fixable")


class PermanentError(ToolError):
    """永久错误：数据不一致、约束冲突等不可恢复的故障。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, "permanent")


def _classify_exception(exc: Exception) -> str:
    """将原始异常映射为 error_type 字符串。"""
    # SQLAlchemy 运维错误（连接断开、超时）-> 可重试
    try:
        from sqlalchemy.exc import OperationalError

        if isinstance(exc, OperationalError):
            return "retryable"
    except ImportError:
        pass

    # 数据完整性冲突 -> 永久
    try:
        from sqlalchemy.exc import IntegrityError

        if isinstance(exc, IntegrityError):
            return "permanent"
    except ImportError:
        pass

    # 值错误/类型错误 -> 用户可修复
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "user_fixable"

    # 权限相关 -> 用户可修复
    if isinstance(exc, PermissionError):
        return "user_fixable"

    # ToolError 子类自带 error_type
    if isinstance(exc, ToolError):
        return exc.error_type

    # 其他未知错误
    return "unknown"


# ------------------------------------------------------------------
# 公共辅助
# ------------------------------------------------------------------
@traced_async(
    "search retrieve",
    attributes={"gen_ai.operation.name": "retrieval"},
)
async def search_photos(
    *,
    user_id: UUID,
    db: AsyncSession,
    query: str,
    from_date: date | None = None,
    to_date: date | None = None,
    tags: list[str] | None = None,
    scene: str | None = None,
    objects: list[str] | None = None,
    text_in_image: list[str] | None = None,
    mood: str | None = None,
    colors: list[str] | None = None,
    photo_types: list[str] | None = None,
    is_selfie: bool | None = None,
    people_count_min: int | None = None,
    people_count_max: int | None = None,
    min_semantic_score: float | None = None,
    status: str | None = "done",
    limit: int = 10,
    cursor: str | None = None,
    exclude_photo_ids: list[str] | None = None,
    auto_parse: bool = True,
    verify_constraints: bool = True,
    verify_semantic: bool = True,
    result_mode: str = "browse",
    complete_result_set: bool = False,
    verified_only: bool = False,
    candidate_pool_size: int = 12,
    force_visual_verify: bool = False,
    include_index_coverage: bool = False,
    w_semantic: float = 0.7,
    w_recency: float = 0.2,
    w_interaction: float = 0.1,
    plan_id: str | None = None,
) -> dict:
    """Compatibility adapter; SearchService owns all search decisions."""
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest, SearchError

    values = dict(locals())
    try:
        request = SearchRequest(
            q=query,
            **{
                key: value
                for key, value in values.items()
                if key in SearchRequest.model_fields
                and key != "q"
                and value is not None
            },
        )
        # None status intentionally includes processing/partial records in fallback.
        request = request.model_copy(update={"status": status})
        result = await SearchService(db, user_id).search(request, plan_id=plan_id)
        return _search_tool_result(result)
    except SearchError as exc:
        return {
            "ok": False,
            "error_type": exc.code,
            "items": [],
            "hint": "搜索状态已变化，请重新搜索或稍后重试",
        }
    except Exception as exc:
        logger.exception("SearchService failed | user=%s", user_id)
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "items": [],
            "hint": "搜索暂时不可用，请稍后重试",
        }


def _search_tool_result(result):
    if result.get("browse_scope") in {"all", "clues"}:
        hint = "这些是浏览候选，未经搜索相关性核验，并非已确认匹配结果"
    elif result.get("items"):
        hint = f"找到 {len(result['items'])} 张相关照片供你选择"
    elif result.get("search_pending") or result.get("stop_reason") == "request_timeout":
        hint = "本次搜索暂停，进度已保存，可以继续查找"
    elif result.get("stop_reason") in {
        "verification_unavailable",
        "verification_incomplete",
        "snapshot_changed",
        "budget_exhausted",
        "deadline_exceeded",
        "candidate_limit",
    }:
        hint = "本轮搜索已达到处理上限，尚不能判断相册中是否还有匹配照片"
    else:
        hint = "当前搜索范围内没有找到更多匹配照片"
    if result.get("coverage_hint"):
        hint += "。" + result["coverage_hint"]
    return {**result, "ok": True, "hint": hint}


# ------------------------------------------------------------------
# Tool 2: 浏览候选照片（最终兜底 / 时间线浏览）
# ------------------------------------------------------------------
async def browse_candidates(
    *,
    user_id: UUID,
    db: AsyncSession,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 50,
    cursor: str | None = None,
    exclude_photo_ids: list[str] | None = None,
) -> dict:
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest, SearchError

    try:
        result = await SearchService(db, user_id).search(
            SearchRequest(
                q=" ",
                retrieval_mode="timeline",
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                cursor=cursor,
                status=None,
                result_mode="select",
                verify_semantic=False,
                verify_constraints=False,
                exclude_photo_ids=exclude_photo_ids or [],
            )
        )
        return _search_tool_result(result)
    except SearchError as exc:
        return {
            "ok": False,
            "error_type": exc.code,
            "items": [],
            "hint": "相册分页已过期或不可用，请重新浏览",
        }


# ------------------------------------------------------------------
# Tool 3: 应用 Skill 做 AI 改造
# ------------------------------------------------------------------
async def apply_skill(
    *,
    user_id: UUID,
    db: AsyncSession,
    photo_id: UUID,
    skill_id: UUID | None = None,
    extra_prompt: str | None = None,
    model: str | None = None,
    idempotency_key: str | None = None,
    require_confirmation: bool = True,
    package_options: dict | None = None,
) -> dict:
    """准备生成任务；灰度组确认后才会预占额度并入队。

    返回：
        {
          "ok": bool,
          "generation_id": str | None,
          "status": str,
          "hint": str,
        }
    """
    try:
        gen = await prepare_generation(
            db=db,
            user_id=user_id,
            photo_id=photo_id,
            skill_id=skill_id,
            extra_prompt=extra_prompt,
            model=model,
            idempotency_key=idempotency_key,
            package_options=package_options,
        )
        if (
            not gen.execution_snapshot
            and not require_confirmation
            and gen.status
            in {
                "awaiting_confirmation",
                "queue_failed",
            }
        ):
            gen = await confirm_generation(
                db=db,
                user_id=user_id,
                generation_id=gen.id,
                confirmation_token=gen.confirmation_token,
            )

        confirmation_required = gen.status == "awaiting_confirmation"

        return {
            "ok": True,
            "generation_id": str(gen.id),
            "status": gen.status,
            "confirmation_required": confirmation_required,
            "confirmation": (
                generation_confirmation_payload(gen) if confirmation_required else None
            ),
            "estimated_cost_yuan": float(gen.estimated_cost_yuan or 0),
            "hint": (
                "请用户确认本次照片、效果和预计费用后再开始生成"
                if confirmation_required
                else "生成任务已提交，稍后可在生成历史中查看结果"
            ),
        }

    except GenerationDomainError as exc:
        return {
            "ok": False,
            "error_type": exc.code,
            "generation_id": None,
            "status": "error",
            "hint": str(exc),
        }
    except Exception as exc:
        logger.exception(
            "apply_skill failed | user=%s photo=%s skill=%s",
            user_id,
            photo_id,
            skill_id,
        )
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "generation_id": None,
            "status": "error",
            "hint": f"生成任务创建失败：{exc}",
        }


# ------------------------------------------------------------------
# Tool 4: 获取单张照片详情（Agent 必要时可调用）
# ------------------------------------------------------------------
async def get_photo_detail(
    *,
    user_id: UUID,
    db: AsyncSession,
    photo_id: UUID,
) -> dict:
    """获取单张照片的完整结构化信息。"""
    try:
        photo = (
            await db.execute(
                select(Photo).where(
                    and_(Photo.id == photo_id, Photo.user_id == user_id)
                )
            )
        ).scalar_one_or_none()
        if photo is None:
            return {
                "ok": False,
                "data": None,
                "hint": "照片不存在或无权访问",
            }

        return {
            "ok": True,
            "data": serialize_photo(photo, (0.0, 0.0, 0.0, 0.0)),
            "hint": "已获取照片详情",
        }
    except Exception as exc:
        logger.exception(
            "get_photo_detail failed | user=%s photo=%s", user_id, photo_id
        )
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "data": None,
            "hint": f"获取照片详情失败：{exc}",
        }


# ------------------------------------------------------------------
# Tool 5: 三级兜底搜索（clue album → timeline → full album）
# ------------------------------------------------------------------
async def fallback_search(
    *,
    user_id: UUID,
    db: AsyncSession,
    query: str,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 30,
    start_level: int = 0,
    exclude_photo_ids: list[str] | None = None,
    allow_unfiltered_browse: bool = True,
    plan_id: str | None = None,
    feedback_level: int | None = None,
    cursor: str | None = None,
) -> dict:
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest, SearchError

    if feedback_level is not None:
        if not plan_id:
            return {"ok": False, "items": [], "error_type": "feedback_plan_missing"}
        try:
            result = await SearchService(db, user_id).feedback_search(
                SearchRequest(
                    q=query,
                    limit=limit,
                    auto_parse=False,
                    exclude_photo_ids=exclude_photo_ids or [],
                ),
                plan_id=plan_id,
                level=feedback_level,
                cursor=cursor,
            )
            if result.get("ok") is False:
                return result
            return {
                **result,
                "ok": True,
                "hint": "以下是范围候选，供手动查找，并非确认匹配。"
                if result.get("browse_scope") == "clues"
                else "已重新核验候选。",
            }
        except SearchError as exc:
            return {"ok": False, "items": [], "error_type": exc.code}

    try:
        result = await SearchService(db, user_id).fallback(
            SearchRequest(
                q=query,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                auto_parse=True,
                exclude_photo_ids=exclude_photo_ids or [],
                include_index_coverage=True,
            ),
            plan_id=plan_id,
            start_level=start_level,
            allow_unfiltered=allow_unfiltered_browse,
        )
        return _search_tool_result(result)
    except SearchError as exc:
        return {
            "ok": False,
            "error_type": exc.code,
            "items": [],
            "hint": "搜索无法继续，请重新搜索或稍后重试",
        }


# ------------------------------------------------------------------
# Tool 6: 主动推荐 Skill
# ------------------------------------------------------------------
async def recommend_skills_for_agent(
    *,
    user_id: UUID,
    db: AsyncSession,
    photo_ids: list[str] | None = None,
    limit: int = 5,
) -> dict:
    """基于用户画像和上下文照片为用户推荐 Skill。

    返回：
        {
          "ok": bool,
          "items": [ {...}, ... ],
          "hint": str,
        }
    """
    try:
        pids = [UUID(pid) for pid in (photo_ids or []) if pid]
        items = await recommend_skills(
            db=db,
            user_id=user_id,
            photo_ids=pids,
            limit=limit,
        )
        return {
            "ok": True,
            "items": items,
            "hint": f"为你推荐 {len(items)} 个 Skill" if items else "暂无匹配 Skill",
        }
    except Exception as exc:
        logger.exception("recommend_skills_for_agent failed | user=%s", user_id)
        return {
            "ok": False,
            "error_type": _classify_exception(exc),
            "items": [],
            "hint": f"推荐失败：{exc}",
        }
