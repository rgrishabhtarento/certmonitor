"""Async SQLAlchemy engine/session wiring.

A single pooled engine is shared per process. The API and the monitoring worker
each create their own, sized independently through the DB_POOL_* settings.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

_engine_kwargs: dict = {
    "echo": settings.DB_ECHO,
    "future": True,
    "pool_pre_ping": True,
}
if _IS_SQLITE:
    # Used by the test suite only; SQLite has no meaningful pooling story.
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
    )

engine: AsyncEngine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Transactional scope for background tasks and CLI entrypoints."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    await engine.dispose()
