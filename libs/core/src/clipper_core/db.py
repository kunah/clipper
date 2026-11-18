from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from clipper_core.config import Settings


def create_engine(settings: Settings, **kwargs) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True, **kwargs)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
