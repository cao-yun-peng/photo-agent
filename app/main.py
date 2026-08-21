"""FastAPI 应用入口 - 生产级增强版.

改进点:
- 统一错误码 + 全局异常处理
- LogID全链路追踪 + JSON结构化日志
- 优雅关闭与资源清理
- 预留Skill热更新入口
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app import __version__
from app.api import api_router
from app.config import settings
from app.core.errors import (
    ApiError,
    INVALID_PARAMS,
    SYSTEM_ERROR,
)
from app.core.logger import get_logger, setup_logging
from app.core.middleware import LogIDMiddleware
from app.core.registry import init_registries
from app.core.telemetry import (
    instrument_fastapi_app,
    setup_telemetry,
    shutdown_telemetry,
)
from app.database import AsyncSessionLocal, engine
from app.services.circuit_breaker import (
    agent_llm_breaker,
    embedding_breaker,
    image_gen_breaker,
    oss_breaker,
    search_rerank_breaker,
    search_visual_verify_breaker,
    vl_breaker,
)
from app.services.lock import get_redis

# ---------- 日志初始化（最先执行）----------
setup_logging(
    log_level=settings.log_level,
    log_dir=settings.log_dir or None,
    json_format=settings.log_json_format,
)
logger = get_logger(__name__)
setup_telemetry(
    service_name=settings.otel_service_name or f"{settings.app_name}-api",
    engine=engine,
)


# ---------- 全局HTTP客户端（复用连接池）----------
_http_client = None


async def _get_http_client():
    """获取全局复用的httpx异步客户端."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        import httpx

        _http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
                keepalive_expiry=60,
            ),
        )
    return _http_client


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期管理."""
    logger.info(
        "photo-agent api starting up | env=%s version=%s",
        settings.app_env,
        __version__,
    )

    # 初始化HTTP连接池
    await _get_http_client()

    # 初始化官方Skill（失败不阻断启动）
    try:
        await _seed_official_skills()
    except Exception:  # noqa: BLE001
        logger.warning("seed official skills skipped (DB not ready?)", exc_info=True)

    # 初始化热更新注册表（Skill/Prompt等）
    try:
        await init_registries()
        logger.info("all registries initialized")
    except Exception:  # noqa: BLE001
        logger.warning("init registries skipped (DB not ready?)", exc_info=True)

    yield

    # ---------- 优雅关闭：按依赖顺序逆序清理资源 ----------
    logger.info("photo-agent api shutting down, cleaning up resources...")

    # 1. 关闭HTTP连接池
    global _http_client
    if _http_client and not _http_client.is_closed:
        try:
            await _http_client.aclose()
            logger.info("http client closed")
        except Exception:  # noqa: BLE001
            logger.warning("error closing http client", exc_info=True)

    # 2. 关闭Redis连接
    try:
        redis = await get_redis()
        await redis.close()
        logger.info("redis connection closed")
    except Exception:  # noqa: BLE001
        logger.warning("error closing redis", exc_info=True)

    # 3. 关闭数据库引擎
    try:
        await engine.dispose()
        logger.info("database engine disposed")
    except Exception:  # noqa: BLE001
        logger.warning("error disposing database engine", exc_info=True)

    logger.info("photo-agent api shutdown complete")
    shutdown_telemetry()


# ---------- 应用实例 ----------
app = FastAPI(
    title="Photo Agent API",
    version=__version__,
    description="中文语境 · 隐私优先 · AI 语义搜索照片管家的后端",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


# ---------- 中间件注册 ----------

# 1. LogID全链路追踪（最先注册，最外层）
app.add_middleware(LogIDMiddleware, app_name=settings.app_name)

# 2. CORS（开发阶段全开，生产上要收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "dev" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Log-ID", "X-Trace-ID", "traceparent"],
)


# ---------- 全局异常处理 ----------


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    """业务异常统一处理."""
    logger.warning(
        "ApiError: code=%d msg=%s",
        exc.error_code.code,
        exc.message,
    )
    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_dict(),
        headers={"X-Log-ID": getattr(_.state, "log_id", "-")},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验失败."""
    errors = exc.errors()
    logger.warning("request validation failed: %s", errors)
    return JSONResponse(
        status_code=422,
        content={
            "errNo": INVALID_PARAMS.code,
            "errMsg": "参数错误",
            "data": {"errors": errors},
        },
        headers={"X-Log-ID": getattr(_.state, "log_id", "-")},
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_error_handler(
    _: Request, exc: ValidationError
) -> JSONResponse:
    """Pydantic数据校验失败."""
    logger.warning("data validation failed: %s", exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "errNo": INVALID_PARAMS.code,
            "errMsg": "数据格式错误",
            "data": {"errors": exc.errors()},
        },
        headers={"X-Log-ID": getattr(_.state, "log_id", "-")},
    )


@app.exception_handler(Exception)
async def unknown_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """未知异常兜底处理."""
    logger.error("unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "errNo": SYSTEM_ERROR.code,
            "errMsg": "系统内部错误" if settings.app_env == "prod" else str(exc),
            "data": None,
        },
        headers={"X-Log-ID": getattr(_.state, "log_id", "-")},
    )


# ---------- 健康检查 ----------
async def _dependency_checks() -> dict[str, str]:
    """检查 readiness 所需的关键依赖。"""
    from sqlalchemy import text

    checks: dict[str, str] = {}
    try:
        redis = await get_redis()
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
        logger.error("redis health check failed: %s", exc)

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {exc}"
        logger.error("database health check failed: %s", exc)
    return checks


@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict:
    dependencies = await _dependency_checks()
    checks: dict = {
        "status": "ok",
        "env": settings.app_env,
        "version": __version__,
        **dependencies,
    }
    if any(value != "ok" for value in dependencies.values()):
        checks["status"] = "degraded"

    # 熔断器状态快照
    checks["circuit_breakers"] = {
        "vl": vl_breaker.to_dict(),
        "embedding": embedding_breaker.to_dict(),
        "agent_llm": agent_llm_breaker.to_dict(),
        "search_rerank": search_rerank_breaker.to_dict(),
        "search_visual_verify": search_visual_verify_breaker.to_dict(),
        "image_gen": image_gen_breaker.to_dict(),
        "oss": oss_breaker.to_dict(),
    }

    return checks


@app.get("/live", tags=["meta"], summary="存活检查（K8s liveness）")
async def live() -> dict:
    """仅证明 API 进程能响应，不访问外部依赖。"""
    return {"status": "ok"}


@app.get("/ready", tags=["meta"], summary="就绪检查（K8s readiness）")
async def ready() -> JSONResponse:
    """数据库和 Redis 均可用时才允许实例接收业务流量。"""
    checks = await _dependency_checks()
    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ok" if is_ready else "not_ready", **checks},
    )


# ---------- 挂载业务路由 ----------
app.include_router(api_router)
instrument_fastapi_app(app)


async def _seed_official_skills() -> None:
    """启动时从 app/data/official_skills.json 读取并 upsert 官方 Skill。

    迭代方式：编辑 app/data/official_skills.json → 重启服务 → 自动更新。
    - 新增的 Skill → 插入（is_official=True, is_public=True）
    - 已存在的官方 Skill → 更新 description/prompt_template/model（保留 use_count）
    - 已存在的用户自建同名 Skill → 跳过，不覆盖
    """
    import json
    from pathlib import Path

    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.skill import Skill

    skills_file = Path(__file__).resolve().parent / "data" / "official_skills.json"
    if not skills_file.is_file():
        logger.warning(
            "official_skills.json not found at %s, skipping seed", skills_file
        )
        return

    with skills_file.open(encoding="utf-8") as f:
        official_skills = json.load(f)

    inserted = 0
    updated = 0
    skipped = 0

    async with AsyncSessionLocal() as db:
        for spec in official_skills:
            existing = (
                await db.execute(select(Skill).where(Skill.name == spec["name"]))
            ).scalar_one_or_none()

            if existing is None:
                # 新增
                db.add(
                    Skill(
                        owner_id=None,
                        name=spec["name"],
                        description=spec.get("description", ""),
                        prompt_template=spec["prompt_template"],
                        reference_keys=spec.get("reference_keys", []),
                        cover_key=spec.get("cover_key"),
                        model=spec.get("model", "wanx2.1-imageedit"),
                        function=spec.get("function", "description_edit"),
                        strength=spec.get("strength", 0.7),
                        is_public=True,
                        is_official=True,
                    )
                )
                inserted += 1
            elif existing.is_official:
                # 更新官方 Skill 内容（保留 use_count / 权限标志）
                existing.description = spec.get("description", existing.description)
                existing.prompt_template = spec["prompt_template"]
                existing.model = spec.get("model", existing.model)
                existing.function = spec.get("function", existing.function)
                existing.strength = spec.get("strength", existing.strength)
                if "reference_keys" in spec:
                    existing.reference_keys = spec["reference_keys"]
                updated += 1
            else:
                # 用户自建同名 Skill，跳过
                skipped += 1

        if inserted or updated:
            await db.commit()

    logger.info(
        "official skills sync: inserted=%d updated=%d skipped=%d total=%d",
        inserted,
        updated,
        skipped,
        len(official_skills),
    )
    logger.notice(
        "official_skills_sync",
        {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "total": len(official_skills),
        },
    )


# ---------- 导出全局HTTP客户端供各服务使用 ----------
def get_global_http_client():
    """获取全局复用的httpx客户端（在lifespan启动后可用）."""
    return _http_client
