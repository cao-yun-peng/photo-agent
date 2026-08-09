"""FastAPI 应用入口."""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router
from app.config import settings
from app.services.circuit_breaker import (
    agent_llm_breaker,
    embedding_breaker,
    image_gen_breaker,
    oss_breaker,
    vl_breaker,
)
from app.services.lock import get_redis

# ---------- 日志 ----------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------- 应用实例 ----------
app = FastAPI(
    title="Photo Agent API",
    version=__version__,
    description="中文语境 · 隐私优先 · AI 语义搜索照片管家的后端",
    docs_url="/docs",
    redoc_url=None,
)


# ---------- CORS（开发阶段全开，生产上要收紧） ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "dev" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 健康检查 ----------
@app.get("/health", tags=["meta"], summary="健康检查")
async def health() -> dict:
    checks = {
        "status": "ok",
        "env": settings.app_env,
        "version": __version__,
    }

    # Redis 连通性检查
    try:
        redis = await get_redis()
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
        checks["status"] = "degraded"

    # 熔断器状态快照
    checks["circuit_breakers"] = {
        "vl": vl_breaker.to_dict(),
        "embedding": embedding_breaker.to_dict(),
        "agent_llm": agent_llm_breaker.to_dict(),
        "image_gen": image_gen_breaker.to_dict(),
        "oss": oss_breaker.to_dict(),
    }

    return checks


# ---------- 挂载业务路由 ----------
app.include_router(api_router)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("photo-agent api starting up | env=%s", settings.app_env)
    # 自动播种/更新官方 Skill（读 JSON 文件，upsert 模式）
    try:
        await _seed_official_skills()
    except Exception:  # noqa: BLE001
        logger.warning("seed official skills skipped (DB not ready?)", exc_info=True)


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
        logger.warning("official_skills.json not found at %s, skipping seed", skills_file)
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
                db.add(Skill(
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
                ))
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
        inserted, updated, skipped, len(official_skills),
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("photo-agent api shutting down")
