"""测试全局配置：强制 mock 模式与测试环境，避免误调真实外部服务."""
from __future__ import annotations

import os

# 必须在任何 app 模块导入前设置，确保 Settings 加载到测试值
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DASHSCOPE_API_KEY", "sk-xxx")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test_secret_for_pytest_only")
os.environ.setdefault("OSS_BUCKET", "photo-agent-dev")
os.environ.setdefault("OSS_KEY_ID", "LTAI_xxx")
