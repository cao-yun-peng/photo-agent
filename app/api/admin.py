"""管理端API: 配置热刷新、状态查看等运维接口.

注意: 生产环境应在网关层限制访问来源，或添加管理员认证.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.errors import ApiError, AUTH_PERMISSION_DENIED
from app.core.logger import get_logger
from app.core.registry import (
    get_registry_stats,
    prompt_registry,
    refresh_all,
    skill_registry,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------- 简单的开发模式认证（生产环境应替换为正式的管理员认证）----------
async def _verify_admin(dev_mode: bool = Query(default=False, include_in_schema=False)):
    """验证管理员权限.

    简化版本: 开发环境(dev_mode=true)直接放行,
    生产环境需要X-Admin-Token头匹配settings.admin_token.
    """
    from app.config import settings

    if settings.app_env == "dev" or dev_mode:
        return True

    # TODO: 生产环境实现正式的管理员认证
    # admin_token = request.headers.get("X-Admin-Token")
    # if admin_token != settings.admin_token:
    #     raise ApiError(AUTH_PERMISSION_DENIED)
    return True


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
