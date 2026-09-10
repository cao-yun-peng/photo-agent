"""Local browser E2E API + worker; hard-wired isolated DB/Redis and no paid providers.

Run alongside Web with NEXT_PUBLIC_API_ORIGIN=http://127.0.0.1:58007.
Automatically stops after 15 minutes. Test accounts remain in the isolated DB.
"""

import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Never read business connection strings, provider or WeChat credentials.
os.environ.update(
    APP_ENV="dev",
    DATABASE_URL="postgresql+asyncpg://batch1:batch1_test_only@127.0.0.1:55439/photo_agent_batch1_test",
    REDIS_URL="redis://127.0.0.1:56389/15",
    JWT_SECRET="p7_isolated_browser_only_secret_never_for_production",
    OSS_BACKEND="mock",
    MOCK_OSS_ENABLED="true",
    DASHSCOPE_API_KEY="",
    OPENAI_API_KEY="",
    WECHAT_APPID="",
    WECHAT_SECRET="",
    ADMIN_ENABLED="false",
    ADMIN_USER_IDS="[]",
    OTEL_ENABLED="false",
    CORS_ORIGINS='["http://127.0.0.1:3002","http://localhost:3002"]',
)


async def main():
    import uvicorn
    from arq import create_pool
    from arq.worker import Worker
    from app.main import app
    from app.services import oss
    from app.workers import tasks

    oss._MOCK_ROOT = ROOT / ".pytest_tmp_p7browser_oss"
    pool = await create_pool(tasks.WorkerSettings.redis_settings)
    tasks._pool = pool
    worker = Worker(
        tasks.WorkerSettings.functions,
        redis_pool=pool,
        handle_signals=False,
        max_jobs=2,
        job_timeout=180,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=58007, access_log=False)
    )
    worker_task = asyncio.create_task(worker.async_run())

    async def deadline():
        await asyncio.sleep(900)
        server.should_exit = True

    deadline_task = asyncio.create_task(deadline())
    try:
        await server.serve()
    finally:
        deadline_task.cancel()
        worker_task.cancel()
        await asyncio.gather(deadline_task, worker_task, return_exceptions=True)
        await worker.close()
        await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
