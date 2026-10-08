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
@pytest.mark.parametrize(
    "bad_phone",
    [2348153551975, None, ["08153551975"], 8.15, {"phone": "0815"}],
    ids=["int", "null", "list", "float", "dict"],
)
async def test_api_register_rejects_non_string_phone(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    bad_phone: object,
) -> None:
    """A non-string phone must be a 422, never a 500.

    normalize_nigerian_phone runs as a BeforeValidator, so it sees the raw input
    before strict type checking. If it does not reject non-strings itself, the
    .strip() call raises AttributeError, which escapes as an unhandled 500 on
    these unauthenticated endpoints.
    """
    client, _, _ = auth_client

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": bad_phone,
            "full_name": "Test User",
            "password": "StrongPassword123!",
            "role": "customer",
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_api_register_rejects_plus_local_phone(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    """'+0801…' is not a valid international format and must not be normalised."""
    client, _, _ = auth_client

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "+08153551975",
            "full_name": "Test User",
            "password": "StrongPassword123!",
            "role": "customer",
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_api_login_success(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    user = User(
        id=uuid.uuid4(),
        phone="2348011112222",
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
        phone="2348011112222",
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


# ─── Cross-format OTP round trip ─────────────────────────────────────────────
#
# test_api_send_otp_success and test_api_verify_otp_success both use "08011112222",
# so they agree even if normalisation is broken at only one end. The property
# that matters is that the two endpoints may receive *different* spellings of the
# same subscriber and still meet on the same Redis key.

_CANONICAL = "2348011112222"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_phone",
    ["08011112222", "080 111 12222", "+2348011112222", "+234-801-111-2222", "2348011112222"],
)
@pytest.mark.parametrize(
    "verify_phone",
    ["08011112222", "080 111 12222", "+2348011112222", "+234-801-111-2222", "2348011112222"],
)
async def test_api_otp_round_trips_across_differing_input_formats(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    send_phone: str,
    verify_phone: str,
) -> None:
    """Issue with one spelling, redeem with another — the code must be found."""
    client, mock_db, redis = auth_client

    send_response = await client.post("/api/v1/auth/otp/send", json={"phone": send_phone})
    assert send_response.status_code == 204, send_response.text

    stored_otp = await redis.get(f"otp:{_CANONICAL}")
    assert stored_otp is not None, f"send({send_phone!r}) did not write the canonical key"

    mock_db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(
            return_value=User(
                id=uuid.uuid4(),
                phone=_CANONICAL,
                full_name="Test User",
                hashed_password="hash",
                role="customer",
                is_active=True,
            )
        )
    )

    verify_response = await client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": verify_phone, "otp": str(stored_otp)},
    )
    assert verify_response.status_code == 200, verify_response.text
    assert "access_token" in verify_response.json()
    assert await redis.get(f"otp:{_CANONICAL}") is None


@pytest.mark.asyncio
async def test_api_redeeming_a_wrong_code_does_not_consume_it(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    """A mistyped code must leave the real one usable."""
    client, _, redis = auth_client
    await redis.set(f"otp:{_CANONICAL}", "123456")

    response = await client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": "080 111 12222", "otp": "654321"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "AUTH_INVALID_OTP"
    assert await redis.get(f"otp:{_CANONICAL}") == "123456"


@pytest.mark.asyncio
async def test_api_register_rejects_non_ascii_digits(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    # Configure the lookup so the request runs the register path to completion —
    # today it is accepted, and an unconfigured AsyncMock would instead leak a
    # "coroutine was never awaited" warning that masks the real failure.
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "2348" + "٨١٥٣٥٥١٩٧",
            "full_name": "Test User",
            "password": "StrongPassword123!",
            "role": "customer",
        },
    )
    assert response.status_code == 422, (
        f"registered a non-ASCII phone: {response.status_code} {response.text}"
    )
