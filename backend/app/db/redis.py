"""Async Redis connection pool and FastAPI dependency."""

from collections.abc import AsyncGenerator
from typing import Annotated

import redis.asyncio as aioredis
import structlog
from app.core.config import Settings, get_settings
from fastapi import Depends

logger = structlog.get_logger(__name__)

_redis_pool: aioredis.ConnectionPool | None = None


def get_redis_pool(settings: Settings) -> aioredis.ConnectionPool:
    """Return the global Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.ConnectionPool.from_url(
            str(settings.REDIS_URL),
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_pool


async def get_redis_client(settings: Settings) -> aioredis.Redis:
    """Create a Redis client from the shared connection pool."""
    pool = get_redis_pool(settings)
    return aioredis.Redis(connection_pool=pool)


async def close_redis() -> None:
    """Disconnect and clean up the Redis connection pool."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None


async def get_redis(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncGenerator[aioredis.Redis, None]:
    """FastAPI dependency that yields a Redis client."""
    client = await get_redis_client(settings)
    try:
        yield client
    finally:
        await client.aclose()
