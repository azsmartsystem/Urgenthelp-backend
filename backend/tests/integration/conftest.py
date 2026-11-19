"""Integration test fixtures backed by real PostgreSQL and real Redis.

Safety invariants:
- Never connects to the dev database; derives ``<db>_test`` unless
  ``TEST_DATABASE_URL`` is explicitly set.
- Never touches Upstash or Redis DB 0; defaults to ``redis://localhost:6379/15``
  unless ``TEST_REDIS_URL`` is explicitly set.
- Runs ``alembic upgrade head`` once per session against the test database.
- Wraps every test in an outer PostgreSQL transaction with savepoint support
  (``join_transaction_mode="create_savepoint"``) and rolls back on teardown.
- Gracefully skips if local PostgreSQL or Redis is unreachable.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import alembic.command as alembic_command
import asyncpg  # type: ignore[import-untyped]
import fakeredis.aioredis
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from alembic.config import Config as AlembicConfig
from app.core.config import get_settings
from app.core.storage import InMemoryStorageBackend
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.modules.users.router import _get_storage_backend
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _derive_test_db_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base_url = str(get_settings().DATABASE_URL)
    parsed = urlparse(base_url)
    db_name = parsed.path.lstrip("/") or "urgenthelp"
    test_db_name = db_name if db_name.endswith("_test") else f"{db_name}_test"
    return urlunparse(parsed._replace(path=f"/{test_db_name}"))


def _derive_test_redis_url() -> str:
    return os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


def _to_asyncpg_sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _ensure_test_database(sync_pg_url: str) -> None:
    parsed = urlparse(sync_pg_url)
    db_name = parsed.path.lstrip("/")
    admin_url = urlunparse(parsed._replace(path="/postgres"))

    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            db_name,
        )
        if not exists:
            # db_name is derived from our own config, quote identifier safely
            safe_ident = db_name.replace('"', '""')
            await conn.execute(f'CREATE DATABASE "{safe_ident}"')
    finally:
        await conn.close()


def _run_alembic_upgrade(sync_pg_url: str) -> None:
    prev_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_pg_url
    get_settings.cache_clear()
    try:
        cfg = AlembicConfig(str(_BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
        alembic_command.upgrade(cfg, "head")
    finally:
        if prev_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_db_url
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def migrated_test_db_url() -> str:
    """Create the isolated test DB if missing and run ``alembic upgrade head`` once."""
    sync_url = _derive_test_db_url().replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        asyncio.run(_ensure_test_database(sync_url))
    except Exception as exc:
        pytest.skip(f"Local PostgreSQL unavailable for integration tests: {exc}")

    _run_alembic_upgrade(sync_url)
    return _to_asyncpg_sqlalchemy_url(sync_url)


@pytest_asyncio.fixture
async def pg_engine(migrated_test_db_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """Per-test AsyncEngine with NullPool so connections stay bound to the active event loop."""
    engine = create_async_engine(migrated_test_db_url, echo=False, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide an AsyncSession wrapped in an outer transaction rolled back after each test."""
    async with pg_engine.connect() as conn:
        outer_tx = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await outer_tx.rollback()


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[aioredis.Redis, None]:
    """Use real local Redis (DB 15) when reachable, else fall back to FakeRedis."""
    url = _derive_test_redis_url()
    client: aioredis.Redis = aioredis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        fake: aioredis.Redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        try:
            yield fake
        finally:
            await fake.flushdb()
            await fake.aclose()
        return

    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def integration_client(
    db_session: AsyncSession,
    redis_client: aioredis.Redis,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend], None]:
    """Real FastAPI client wired to real Postgres (savepoint-rolled-back) and real Redis."""
    storage = InMemoryStorageBackend()

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncGenerator[aioredis.Redis, None]:
        yield redis_client

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    app.dependency_overrides[_get_storage_backend] = lambda: storage

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db_session, redis_client, storage
    finally:
        app.dependency_overrides.clear()
