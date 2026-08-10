"""API 路由聚合."""
from fastapi import APIRouter

from app.api import _oss_mock, admin, agent, auth, generations, photos, search, skills

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(photos.router, prefix="/photos", tags=["photos"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(skills.router, prefix="/skills", tags=["skills"])
api_router.include_router(agent.router, prefix="/agent", tags=["agent"])
# generations 的路径分散在 /photos/{id}/generate 和 /generations 下，不加 prefix
api_router.include_router(generations.router, tags=["generations"])
# 管理端API: 热更新、状态查看
api_router.include_router(admin.router, tags=["admin"])
# 开发环境用的假 OSS 端点；生产时 is_mock() 为 False，路由会自己拒绝
api_router.include_router(_oss_mock.router)
