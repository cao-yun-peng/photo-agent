"""Tests never inherit model credentials or production connection strings from .env."""

import os

os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": os.environ.get(
            "PHOTO_AGENT_TEST_DATABASE_URL",
            "postgresql+asyncpg://batch1:batch1_test_only@127.0.0.1:55439/photo_agent_batch1_test",
        ),
        "REDIS_URL": os.environ.get(
            "PHOTO_AGENT_TEST_REDIS_URL", "redis://127.0.0.1:56389/15"
        ),
        "JWT_SECRET": "batch1_test_only_secret_never_for_production",
        "OSS_BACKEND": "mock",
        "MOCK_OSS_ENABLED": "true",
        "DASHSCOPE_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ADMIN_ENABLED": "false",
        "ADMIN_USER_IDS": "[]",
        "OTEL_ENABLED": "false",
    }
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: isolated PostgreSQL and Redis required"
    )
