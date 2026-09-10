"""管理端API: 配置热刷新、状态查看等运维接口.

默认关闭；启用后需要 JWT 和服务端管理员白名单。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.core.security import get_current_user
from app.models.user import User
from app.core.logger import get_logger
from app.core.registry import (
    get_registry_stats,
    prompt_registry,
    refresh_all,
    skill_registry,
)

logger = get_logger(__name__)


async def _verify_admin(user: User = Depends(get_current_user)):
    if not settings.admin_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    if user.id not in settings.admin_user_ids:
        raise HTTPException(status_code=403, detail="Administrator permission required")
    logger.warning("admin access authorized | user=%s", user.id)
    return True


router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(_verify_admin)]
)


@router.post("/refresh", summary="刷新所有配置（热更新）")
async def refresh_config(
    reason: str = Query(default="admin_publish", description="刷新原因"),
    _: bool = Depends(_verify_admin),
) -> dict:
    """触发全量配置热刷新.

    包括:
    - Skill注册表（从DB重新加载）
    - Prompt注册表（从配置文件重新加载）

    刷新过程:
    1. 获取全局锁，防止并发刷新
    2. 构建新数据
    3. 原子替换引用（进行中的请求不受影响）
    """
    logger.warning("admin config refresh triggered, reason=%s", reason)
    results = await refresh_all(reason=reason)

    success = all(results.values())
    return {
        "errNo": 0,
        "errMsg": "刷新成功" if success else "部分刷新失败",
        "data": {
            "results": results,
            "stats": get_registry_stats(),
        },
    }


@router.post("/refresh/skills", summary="刷新Skill注册表")
async def refresh_skills(
    reason: str = Query(default="admin_publish_skills"),
    _: bool = Depends(_verify_admin),
) -> dict:
    """单独刷新Skill注册表."""
    logger.warning("admin skill refresh triggered, reason=%s", reason)
    success = await skill_registry.refresh(reason=reason)
    return {
        "errNo": 0 if success else -2,
        "errMsg": "Skill刷新成功" if success else "Skill刷新失败",
        "data": skill_registry.get_stats(),
    }


@router.post("/refresh/prompts", summary="刷新Prompt注册表")
async def refresh_prompts(
    reason: str = Query(default="admin_publish_prompts"),
    _: bool = Depends(_verify_admin),
) -> dict:
    """单独刷新Prompt注册表."""
    logger.warning("admin prompt refresh triggered, reason=%s", reason)
    success = await prompt_registry.refresh(reason=reason)
    return {
        "errNo": 0 if success else -2,
        "errMsg": "Prompt刷新成功" if success else "Prompt刷新失败",
        "data": prompt_registry.get_stats(),
    }


@router.get("/stats", summary="查看注册表状态")
async def registry_stats(_: bool = Depends(_verify_admin)) -> dict:
    """查看所有注册表当前状态（用于监控）."""
    return {
        "errNo": 0,
        "errMsg": "ok",
        "data": get_registry_stats(),
    }
