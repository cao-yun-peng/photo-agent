"""SQLAlchemy 异步引擎与 Session 工厂."""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类."""


engine = create_async_engine(
    settings.database_url,
    echo=settings.app_env == "dev",
    hide_parameters=True,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)


class OwnedAsyncSession(AsyncSession):
    async def commit(self) -> None:
        guard = self.info.get("commit_guard")
        if guard is not None:
            try:
                await guard(self)
            except BaseException:
                await self.rollback()
                raise
        await super().commit()


AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=OwnedAsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends 用的数据库会话生成器."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
