"""Integration tests for Auth API endpoints."""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.security import create_refresh_token, hash_password
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.modules.users.model import User
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fake_redis_instance() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def test_settings() -> Settings:
    return get_settings()


@pytest.fixture
async def auth_client(
    fake_redis_instance: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
) -> AsyncGenerator[tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis], None]:
    mock_db = AsyncMock()
    mock_db.add = MagicMock()

    async def override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    async def override_get_redis() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
        yield fake_redis_instance

    def override_get_settings() -> Settings:
        return test_settings

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    app.dependency_overrides[get_settings] = override_get_settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, mock_db, fake_redis_instance

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_api_register_success(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    payload = {
        "phone": "+2348011112222",
        "full_name": "Test User",
        "password": "StrongPassword123!",
        "role": "customer",
        "email": "test@example.com",
    }

    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "Bearer"


@pytest.mark.asyncio
async def test_api_login_success(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    user = User(
        id=uuid.uuid4(),
        phone="+2348011112222",
        full_name="Test User",
        hashed_password=hash_password("StrongPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = {
        "phone": "+2348011112222",
        "password": "StrongPassword123!",
    }

    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_api_send_otp_success(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, redis = auth_client
    payload = {"phone": "08011112222"}

    response = await client.post("/api/v1/auth/otp/send", json=payload)
    assert response.status_code == 204

    stored_otp = await redis.get("otp:2348011112222")
    assert stored_otp is not None


@pytest.mark.asyncio
async def test_api_verify_otp_success(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    canonical_phone = "2348011112222"
    otp = "123456"
    await redis.set(f"otp:{canonical_phone}", otp)

    user = User(
        id=uuid.uuid4(),
        phone=canonical_phone,
        full_name="Test User",
        hashed_password="hash",
        role="customer",
        is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    # Send in local format 080... to ensure normalization works on verify
    payload = {"phone": "08011112222", "otp": otp}
    response = await client.post("/api/v1/auth/otp/verify", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_api_refresh_tokens_success(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, mock_db, _ = auth_client
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="+2348011112222",
        full_name="Test User",
        hashed_password="hash",
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    refresh_token = create_refresh_token(user_id, "customer", test_settings)

    payload = {"refresh_token": refresh_token}
    response = await client.post("/api/v1/auth/refresh", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
