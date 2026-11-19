"""Integration tests for Auth API endpoints."""

import hashlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.security import create_access_token, create_refresh_token, hash_password
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
    client, mock_db, redis = auth_client
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
    assert "id" in data
    assert data["phone"] == "2348011112222"
    assert data["role"] == "customer"
    assert data["full_name"] == "Test User"
    assert data["email"] == "test@example.com"
    assert data["is_phone_verified"] is False
    assert data["is_email_verified"] is False
    assert "access_token" not in data
    assert "refresh_token" not in data

    stored_otp = await redis.get("otp:2348011112222")
    assert stored_otp is not None
    stored_email_otp = await redis.get(f"email_otp:{data['id']}")
    assert stored_email_otp is not None


@pytest.mark.asyncio
async def test_api_register_requires_email(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "+2348011112222",
            "full_name": "Test User",
            "password": "StrongPassword123!",
            "role": "customer",
        },
    )
    assert response.status_code == 422


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
        email="test@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Test User",
        hashed_password=hash_password("StrongPassword123!"),
        role="customer",
        is_active=True,
        is_phone_verified=True,
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
async def test_api_login_unverified_phone_returns_403(
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
        is_phone_verified=False,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = {
        "phone": "+2348011112222",
        "password": "StrongPassword123!",
    }

    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 403
    data = response.json()
    assert data["code"] == "AUTH_PHONE_NOT_VERIFIED"


@pytest.mark.asyncio
async def test_api_login_unverified_email_returns_403(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    user = User(
        id=uuid.uuid4(),
        phone="2348011112222",
        email="unverified@example.com",
        email_verified_at=None,
        full_name="Test User",
        hashed_password=hash_password("StrongPassword123!"),
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    response = await client.post(
        "/api/v1/auth/login",
        json={"phone": "+2348011112222", "password": "StrongPassword123!"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_EMAIL_NOT_VERIFIED"


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
async def test_api_send_otp_cooldown_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client
    payload = {"phone": "08011112222"}

    # First request succeeds
    res1 = await client.post("/api/v1/auth/otp/send", json=payload)
    assert res1.status_code == 204

    # Second request immediately triggers cooldown
    res2 = await client.post("/api/v1/auth/otp/send", json=payload)
    assert res2.status_code == 429
    data = res2.json()
    assert data["code"] == "AUTH_OTP_COOLDOWN"
    assert "Retry-After" in res2.headers


@pytest.mark.asyncio
async def test_api_send_otp_rate_limit_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, _, redis = auth_client
    payload = {"phone": "08011112222"}
    canonical = "2348011112222"

    for _ in range(test_settings.OTP_MAX_REQUESTS_PER_WINDOW):
        await redis.delete(f"otp:cooldown:{canonical}")
        res = await client.post("/api/v1/auth/otp/send", json=payload)
        assert res.status_code == 204

    # Next attempt triggers rate limit even without deleting cooldown
    res_limited = await client.post("/api/v1/auth/otp/send", json=payload)
    assert res_limited.status_code == 429
    data = res_limited.json()
    assert data["code"] == "AUTH_OTP_RATE_LIMITED"
    assert "Retry-After" in res_limited.headers


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
        email="test@example.com",
        email_verified_at=datetime.now(UTC),
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
    assert user.is_phone_verified is True
    assert user.phone_verified_at is not None
    assert mock_db.commit.called


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
                email="test@example.com",
                email_verified_at=datetime.now(UTC),
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


@pytest.mark.asyncio
async def test_api_full_registration_verification_login_flow(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    """Lifecycle: register (no tokens) -> verify phone & email -> login (200)."""
    client, mock_db, redis = auth_client

    # 1. Register: phone not taken, email not taken
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    reg_payload = {
        "phone": "+2348012345678",
        "email": "Lifecycle@example.com",
        "full_name": "Full Lifecycle User",
        "password": "SecurePassword123!",
        "role": "customer",
    }
    reg_res = await client.post("/api/v1/auth/register", json=reg_payload)
    assert reg_res.status_code == 201
    reg_data = reg_res.json()
    assert reg_data["is_phone_verified"] is False
    assert reg_data["is_email_verified"] is False
    assert reg_data["email"] == "lifecycle@example.com"
    assert "access_token" not in reg_data

    # Extract user created and OTPs from redis
    user_created = mock_db.add.call_args[0][0]
    assert user_created.phone == "2348012345678"
    assert user_created.email == "lifecycle@example.com"
    assert not user_created.is_phone_verified
    assert not user_created.email_verified_at

    canonical_phone = "2348012345678"
    otp = await redis.get(f"otp:{canonical_phone}")
    email_otp = await redis.get(f"email_otp:{user_created.id}")
    assert otp is not None
    assert email_otp is not None

    # 2. Try password login before verification -> 403 AUTH_PHONE_NOT_VERIFIED
    mock_db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=user_created)
    )
    login_payload = {
        "phone": "+2348012345678",
        "password": "SecurePassword123!",
    }
    login_fail_res = await client.post("/api/v1/auth/login", json=login_payload)
    assert login_fail_res.status_code == 403
    assert login_fail_res.json()["code"] == "AUTH_PHONE_NOT_VERIFIED"

    # 3. Verify phone OTP -> phone verified, blocked on unverified email (no tokens yet)
    verify_payload = {"phone": "08012345678", "otp": str(otp)}
    verify_res = await client.post("/api/v1/auth/otp/verify", json=verify_payload)
    assert verify_res.status_code == 403
    assert verify_res.json()["code"] == "AUTH_EMAIL_NOT_VERIFIED"
    assert user_created.is_phone_verified is True
    assert user_created.phone_verified_at is not None

    # 4. Password login still blocked until email is verified -> 403 AUTH_EMAIL_NOT_VERIFIED
    login_unverified_email = await client.post("/api/v1/auth/login", json=login_payload)
    assert login_unverified_email.status_code == 403
    assert login_unverified_email.json()["code"] == "AUTH_EMAIL_NOT_VERIFIED"

    # 5. Verify email OTP -> 200 OK with tokens
    email_verify_res = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "lifecycle@example.com", "otp": str(email_otp)},
    )
    assert email_verify_res.status_code == 200
    verify_data = email_verify_res.json()
    assert "access_token" in verify_data
    assert "refresh_token" in verify_data
    assert user_created.email_verified_at is not None

    # 6. Now password login succeeds -> 200 OK with tokens
    login_success_res = await client.post("/api/v1/auth/login", json=login_payload)
    assert login_success_res.status_code == 200
    login_data = login_success_res.json()
    assert "access_token" in login_data
    assert "refresh_token" in login_data


@pytest.mark.asyncio
async def test_api_email_send_and_verify_before_phone_verification(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    """Verifying email first persists email_verified_at; phone verify then issues tokens."""
    client, mock_db, redis = auth_client
    user = User(
        id=uuid.uuid4(),
        phone="2348012345678",
        email="first@example.com",
        email_verified_at=None,
        full_name="Email First",
        hashed_password=hash_password("SecurePassword123!"),
        role="customer",
        is_active=True,
        is_phone_verified=False,
    )
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=user))

    send_res = await client.post("/api/v1/auth/email/send", json={"email": "first@example.com"})
    assert send_res.status_code == 204
    email_otp = await redis.get(f"email_otp:{user.id}")
    assert email_otp is not None

    # Email verified before phone -> 403 AUTH_PHONE_NOT_VERIFIED, email_verified_at persisted
    verify_email_res = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "first@example.com", "otp": str(email_otp)},
    )
    assert verify_email_res.status_code == 403
    assert verify_email_res.json()["code"] == "AUTH_PHONE_NOT_VERIFIED"
    assert user.email_verified_at is not None

    # Phone OTP verify now returns tokens
    await redis.set("otp:2348012345678", "654321")
    verify_phone_res = await client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": "08012345678", "otp": "654321"},
    )
    assert verify_phone_res.status_code == 200
    assert "access_token" in verify_phone_res.json()


# ─── Forgot and Reset Password Integration Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_api_forgot_password_verified_email_sends_reset_token(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348011112222",
        email="verified@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Verified User",
        hashed_password=hash_password("StrongPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=user))

    payload = {"email": "verified@example.com"}
    response = await client.post("/api/v1/auth/password/forgot", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "instructions have been sent" in data["message"]

    keys = await redis.keys("pwd_reset:token:*")
    assert len(keys) == 1
    assert await redis.get(keys[0]) == str(user_id)
    assert await redis.get(f"pwd_reset:active:{user_id}") is not None


@pytest.mark.asyncio
async def test_api_forgot_password_unverified_email_blocked_anti_enumeration(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348011112222",
        email="unverified@example.com",
        email_verified_at=None,
        full_name="Unverified User",
        hashed_password=hash_password("StrongPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=user))

    payload = {"email": "unverified@example.com"}
    response = await client.post("/api/v1/auth/password/forgot", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "instructions have been sent" in data["message"]

    assert await redis.keys("pwd_reset:token:*") == []
    assert await redis.get(f"pwd_reset:active:{user_id}") is None


@pytest.mark.asyncio
async def test_api_forgot_password_reissue_revokes_previous_token(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348011112222",
        email="verified@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Verified User",
        hashed_password=hash_password("StrongPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=user))

    res1 = await client.post("/api/v1/auth/password/forgot", json={"email": "verified@example.com"})
    assert res1.status_code == 200
    first_keys = await redis.keys("pwd_reset:token:*")
    assert len(first_keys) == 1
    first_token_key = first_keys[0]

    await redis.delete("pwd_reset:cooldown:verified@example.com")
    res2 = await client.post("/api/v1/auth/password/forgot", json={"email": "verified@example.com"})
    assert res2.status_code == 200
    second_keys = await redis.keys("pwd_reset:token:*")
    assert len(second_keys) == 1
    assert second_keys[0] != first_token_key
    assert await redis.get(first_token_key) is None


@pytest.mark.asyncio
async def test_api_forgot_password_non_existent_account_returns_200(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    payload = {"email": "missing@example.com"}
    response = await client.post("/api/v1/auth/password/forgot", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "instructions have been sent" in data["message"]


@pytest.mark.asyncio
async def test_api_forgot_password_cooldown_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    payload = {"email": "user@example.com"}
    res1 = await client.post("/api/v1/auth/password/forgot", json=payload)
    assert res1.status_code == 200

    res2 = await client.post("/api/v1/auth/password/forgot", json=payload)
    assert res2.status_code == 429
    data = res2.json()
    assert data["code"] == "AUTH_PASSWORD_RESET_COOLDOWN"
    assert "Retry-After" in res2.headers


@pytest.mark.asyncio
async def test_api_forgot_password_rate_limit_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    email = "user@example.com"
    payload = {"email": email}

    for _ in range(3):
        await redis.delete(f"pwd_reset:cooldown:{email}")
        res = await client.post("/api/v1/auth/password/forgot", json=payload)
        assert res.status_code == 200

    res_limited = await client.post("/api/v1/auth/password/forgot", json=payload)
    assert res_limited.status_code == 429
    data = res_limited.json()
    assert data["code"] == "AUTH_PASSWORD_RESET_RATE_LIMITED"
    assert "Retry-After" in res_limited.headers


@pytest.mark.asyncio
async def test_api_forgot_password_ip_rate_limit_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    for i in range(10):
        email = f"user{i}@example.com"
        await redis.delete(f"pwd_reset:cooldown:{email}")
        res = await client.post("/api/v1/auth/password/forgot", json={"email": email})
        assert res.status_code == 200

    res_ip = await client.post("/api/v1/auth/password/forgot", json={"email": "user10@example.com"})
    assert res_ip.status_code == 429
    assert res_ip.json()["code"] == "AUTH_PASSWORD_RESET_RATE_LIMITED"


@pytest.mark.asyncio
async def test_api_forgot_password_forwarded_ip_rate_limit_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    forwarded_headers = {"x-forwarded-for": "203.0.113.195, 70.41.3.18"}

    for i in range(10):
        email = f"fwd{i}@example.com"
        await redis.delete(f"pwd_reset:cooldown:{email}")
        res = await client.post(
            "/api/v1/auth/password/forgot",
            json={"email": email},
            headers=forwarded_headers,
        )
        assert res.status_code == 200

    res_forwarded = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "fwd10@example.com"},
        headers=forwarded_headers,
    )
    assert res_forwarded.status_code == 429
    assert res_forwarded.json()["code"] == "AUTH_PASSWORD_RESET_RATE_LIMITED"

    # Also test whitespace X-Forwarded-For falls back to client host gracefully
    await redis.delete("pwd_reset:cooldown:fwd11@example.com")
    res_whitespace = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "fwd11@example.com"},
        headers={"x-forwarded-for": "   "},
    )
    assert res_whitespace.status_code == 200


@pytest.mark.asyncio
async def test_api_forgot_password_rejects_empty_phone_only_and_invalid_inputs(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client

    res_empty = await client.post("/api/v1/auth/password/forgot", json={})
    assert res_empty.status_code == 422

    res_phone_only = await client.post(
        "/api/v1/auth/password/forgot", json={"phone": "08011112222"}
    )
    assert res_phone_only.status_code == 422

    res_bad_email = await client.post(
        "/api/v1/auth/password/forgot", json={"email": "not-an-email"}
    )
    assert res_bad_email.status_code == 422


@pytest.mark.asyncio
async def test_api_reset_password_success_flow(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, mock_db, redis = auth_client
    user_id = uuid.uuid4()
    old_password = "OldPassword123!"
    new_password = "BrandNewPassword123!"

    user = User(
        id=user_id,
        phone="2348011112222",
        email="reset@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Reset User",
        hashed_password=hash_password(old_password),
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )
    mock_db.get.return_value = user
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=user))

    # Issue refresh token before reset
    old_refresh_token = create_refresh_token(
        user_id,
        "customer",
        test_settings,
        issued_at=datetime.now(UTC) - timedelta(hours=1),
    )

    # Put reset token in redis
    raw_token = "secret_reset_token_abc"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    # 1. Reset password
    reset_res = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": raw_token, "new_password": new_password},
    )
    assert reset_res.status_code == 200
    assert reset_res.json()["message"] == "Password has been reset successfully."

    # Token must be deleted
    assert await redis.get(f"pwd_reset:token:{token_hash}") is None

    # 2. Prior refresh token is now revoked
    refresh_fail_res = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh_token},
    )
    assert refresh_fail_res.status_code == 401
    assert "revoked" in refresh_fail_res.json()["detail"].lower()

    # 3. Old password cannot login
    login_fail_res = await client.post(
        "/api/v1/auth/login",
        json={"phone": "08011112222", "password": old_password},
    )
    assert login_fail_res.status_code == 401

    # 4. New password can login
    login_ok_res = await client.post(
        "/api/v1/auth/login",
        json={"phone": "08011112222", "password": new_password},
    )
    assert login_ok_res.status_code == 200
    new_refresh = login_ok_res.json()["refresh_token"]

    # 5. New refresh token works
    refresh_ok_res = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": new_refresh},
    )
    assert refresh_ok_res.status_code == 200


@pytest.mark.asyncio
async def test_api_reset_password_invalid_token_returns_400(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client

    response = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": "expired_or_invalid_token", "new_password": "NewPassword123!"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "AUTH_INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_api_reset_password_weak_password_returns_422(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client

    # Lacks uppercase
    res1 = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": "token123", "new_password": "lowercaseonly123"},
    )
    assert res1.status_code == 422

    # Lacks digit
    res2 = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": "token123", "new_password": "NoDigitsInHere!"},
    )
    assert res2.status_code == 422


@pytest.mark.asyncio
async def test_api_reset_password_brute_force_lockout_returns_429(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client

    for _ in range(5):
        res = await client.post(
            "/api/v1/auth/password/reset",
            json={"token": "invalid_token", "new_password": "NewPassword123!"},
        )
        assert res.status_code == 400

    # 6th attempt is locked out
    res_lockout = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": "invalid_token", "new_password": "NewPassword123!"},
    )
    assert res_lockout.status_code == 429
    assert res_lockout.json()["code"] == "AUTH_PASSWORD_RESET_RATE_LIMITED"


@pytest.mark.asyncio
async def test_api_reset_password_strips_token_whitespace(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, redis = auth_client
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348011112222",
        email="reset@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Reset User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )
    mock_db.get.return_value = user

    raw_token = "secret_reset_token_with_spaces"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await redis.set(f"pwd_reset:token:{token_hash}", str(user_id))
    await redis.set(f"pwd_reset:active:{user_id}", token_hash)

    reset_res = await client.post(
        "/api/v1/auth/password/reset",
        json={
            "token": f"  {raw_token}  ",
            "new_password": "NewStrongPassword123!",
        },
    )
    assert reset_res.status_code == 200
    assert reset_res.json()["message"] == "Password has been reset successfully."
    assert await redis.get(f"pwd_reset:active:{user_id}") is None


@pytest.mark.asyncio
async def test_api_logout_success_and_blocks_subsequent_refresh(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, _, redis = auth_client
    user_id = uuid.uuid4()
    refresh_token = create_refresh_token(user_id, "customer", test_settings)

    logout_res = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout_res.status_code == 204
    assert logout_res.content == b""
    assert await redis.get(f"blacklist:{refresh_token}") == "1"

    refresh_res = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh_res.status_code == 401
    assert refresh_res.json()["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.asyncio
async def test_api_logout_is_idempotent(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, _, _ = auth_client
    refresh_token = create_refresh_token(uuid.uuid4(), "customer", test_settings)

    first = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    second = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert first.status_code == 204
    assert second.status_code == 204


@pytest.mark.asyncio
async def test_api_logout_expired_refresh_token_returns_204(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, _, redis = auth_client
    expired_iat = datetime.now(UTC) - timedelta(days=test_settings.JWT_REFRESH_EXPIRE_DAYS + 1)
    expired_refresh = create_refresh_token(
        uuid.uuid4(), "customer", test_settings, issued_at=expired_iat
    )

    res = await client.post("/api/v1/auth/logout", json={"refresh_token": expired_refresh})
    assert res.status_code == 204
    assert await redis.get(f"blacklist:{expired_refresh}") is None


@pytest.mark.asyncio
async def test_api_logout_invalid_or_access_token_returns_401(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
    test_settings: Settings,
) -> None:
    client, _, _ = auth_client

    malformed = await client.post("/api/v1/auth/logout", json={"refresh_token": "not.a.jwt"})
    assert malformed.status_code == 401
    assert malformed.json()["code"] == "AUTH_INVALID_TOKEN"

    access_token = create_access_token(uuid.uuid4(), "customer", test_settings)
    wrong_type = await client.post("/api/v1/auth/logout", json={"refresh_token": access_token})
    assert wrong_type.status_code == 401
    assert wrong_type.json()["code"] == "AUTH_INVALID_TOKEN"


# ─── Phone Pre-Check Integration Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_api_phone_check_normalizes_and_reports_availability(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client

    # Unregistered phone (various Nigerian formats normalize identically)
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    res = await client.post("/api/v1/auth/phone/check", json={"phone": "0815 355 1975"})
    assert res.status_code == 200
    assert res.json() == {"phone": "2348153551975", "is_registered": False}

    # Registered phone
    mock_db.execute.return_value = MagicMock(
        scalar_one_or_none=MagicMock(return_value=uuid.uuid4())
    )
    res_taken = await client.post("/api/v1/auth/phone/check", json={"phone": "+2348153551975"})
    assert res_taken.status_code == 200
    assert res_taken.json() == {"phone": "2348153551975", "is_registered": True}


@pytest.mark.asyncio
async def test_api_phone_check_rejects_invalid_nigerian_phone(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, _, _ = auth_client

    for bad_phone in ("06012345678", "12345", "+14155551234", ""):
        res = await client.post("/api/v1/auth/phone/check", json={"phone": bad_phone})
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_api_phone_check_enforces_ip_rate_limit(
    auth_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis],
) -> None:
    client, mock_db, _ = auth_client
    mock_db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

    headers = {"x-forwarded-for": "198.51.100.42"}
    for _ in range(10):
        ok = await client.post(
            "/api/v1/auth/phone/check",
            json={"phone": "+2348153551975"},
            headers=headers,
        )
        assert ok.status_code == 200

    blocked = await client.post(
        "/api/v1/auth/phone/check",
        json={"phone": "+2348153551975"},
        headers=headers,
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "AUTH_PHONE_CHECK_RATE_LIMITED"

    # A different client IP is unaffected
    other_ip = await client.post(
        "/api/v1/auth/phone/check",
        json={"phone": "+2348153551975"},
        headers={"x-forwarded-for": "198.51.100.99"},
    )
    assert other_ip.status_code == 200
