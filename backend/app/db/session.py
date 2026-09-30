"""Async SQLAlchemy session management."""

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

# Convert postgresql:// → postgresql+asyncpg:// for async driver
_db_url = str(settings.DATABASE_URL).replace(
    "postgresql://", "postgresql+asyncpg://", 1
)

engine = create_async_engine(
    _db_url,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,   # detect stale connections
    echo=settings.DEBUG,  # log SQL in dev only
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """Called at startup — verifies DB connectivity."""
    async with engine.connect() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
    logger.info("database_connected")


async def close_db() -> None:
    """Called at shutdown — disposes the connection pool."""
    await engine.dispose()
    logger.info("database_disconnected")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
