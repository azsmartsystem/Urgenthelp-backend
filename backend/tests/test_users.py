"""Integration tests for Users API endpoints (Email verification lifecycle)."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.modules.notifications.email import MockEmailProvider
from app.modules.notifications.service import NotificationService
from app.modules.users.model import User
from app.modules.users.router import _get_notification_service, _get_service
from app.modules.users.service import UserService
from httpx import ASGITransport, AsyncClient
from jose import jwt


@pytest.fixture
def fake_redis_instance() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def test_settings() -> Settings:
    return get_settings()


@pytest.fixture
def mock_email_provider() -> MockEmailProvider:
    return MockEmailProvider()


@pytest.fixture
async def users_client(
    fake_redis_instance: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_email_provider: MockEmailProvider,
) -> AsyncGenerator[
    tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider], None
]:
    mock_db = AsyncMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    notification_service = NotificationService(
        email_provider=mock_email_provider,
        settings=test_settings,
    )

    async def override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    async def override_get_redis() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
        yield fake_redis_instance

    def override_get_settings() -> Settings:
        return test_settings

    def override_get_service() -> UserService:
        return UserService(
            db=mock_db,
            redis_client=fake_redis_instance,
            settings=test_settings,
            notification_service=notification_service,
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    app.dependency_overrides[get_settings] = override_get_settings
    app.dependency_overrides[_get_service] = override_get_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, mock_db, fake_redis_instance, mock_email_provider

    app.dependency_overrides.clear()


def _make_user(
    user_id: uuid.UUID,
    email: str | None = None,
    email_verified_at: datetime | None = None,
    is_active: bool = True,
) -> User:
    return User(
        id=user_id,
        phone="2348011112222",
        email=email,
        full_name="Verified Test User",
        hashed_password="hashed_pwd",
        role="customer",
        is_active=is_active,
        is_id_verified=False,
        is_phone_verified=True,
        email_verified_at=email_verified_at,
        trust_score=50.0,
    )


# ─── Authentication Gate Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_email_endpoints_require_authentication(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
) -> None:
    client, _, _, _ = users_client

    # Without token
    resp_req = await client.post("/api/v1/users/me/email", json={"email": "test@example.com"})
    assert resp_req.status_code == 401
    assert resp_req.json()["code"] == "AUTH_INVALID_TOKEN"

    resp_ver = await client.post("/api/v1/users/me/email/verify", json={"otp": "123456"})
    assert resp_ver.status_code == 401
    assert resp_ver.json()["code"] == "AUTH_INVALID_TOKEN"

    # With malformed token
    bad_headers = {"Authorization": "Bearer not-a-valid-jwt"}
    resp_bad = await client.post(
        "/api/v1/users/me/email", json={"email": "test@example.com"}, headers=bad_headers
    )
    assert resp_bad.status_code == 401
    assert resp_bad.json()["code"] == "AUTH_INVALID_TOKEN"


# ─── POST /api/v1/users/me/email Tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_api_request_email_verification_success(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, email_provider = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    payload = {"email": "new.user@example.com"}
    response = await client.post("/api/v1/users/me/email", json=payload, headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Verification code sent to your email"
    assert data["email"] == "new.user@example.com"

    # Verify Redis keys
    otp = await redis.get(f"email_otp:{user_id}")
    assert otp is not None
    assert len(otp) == 6
    assert (await redis.get(f"pending_email:{user_id}")) == "new.user@example.com"
    assert (await redis.get(f"email:cooldown:{user_id}")) == "1"

    # Verify email was dispatched via provider
    assert len(email_provider.sent_emails) == 1
    assert email_provider.sent_emails[0]["to"] == "new.user@example.com"
    assert email_provider.sent_emails[0]["body"] is not None
    assert str(otp) in str(email_provider.sent_emails[0]["body"])


@pytest.mark.asyncio
async def test_api_request_email_verification_email_already_taken(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, _, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    other_user = _make_user(user_id=uuid.uuid4(), email="claimed@example.com")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = other_user
    mock_db.execute.return_value = mock_result

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/users/me/email", json={"email": "claimed@example.com"}, headers=headers
    )
    assert response.status_code == 409
    data = response.json()
    assert data["code"] == "AUTH_EMAIL_TAKEN"


@pytest.mark.asyncio
async def test_api_request_email_verification_cooldown_returns_429(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    # Set cooldown key
    await redis.set(f"email:cooldown:{user_id}", "1", ex=60)

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/users/me/email", json={"email": "repeat@example.com"}, headers=headers
    )
    assert response.status_code == 429
    data = response.json()
    assert data["code"] == "AUTH_EMAIL_COOLDOWN"
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_api_request_email_verification_rate_limit_returns_429(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    # Max out rate limit
    await redis.set(
        f"email:rate:{user_id}",
        str(test_settings.EMAIL_MAX_REQUESTS_PER_WINDOW),
        ex=300,
    )

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/users/me/email", json={"email": "rate@example.com"}, headers=headers
    )
    assert response.status_code == 429
    data = response.json()
    assert data["code"] == "AUTH_EMAIL_RATE_LIMITED"
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0


# ─── POST /api/v1/users/me/email/verify Tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_api_verify_email_success(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, email=None, email_verified_at=None)
    mock_db.get.return_value = user

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    # Store OTP and pending email in Redis
    await redis.set(f"email_otp:{user_id}", "654321")
    await redis.set(f"pending_email:{user_id}", "verify.me@example.com")

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/users/me/email/verify", json={"otp": "654321"}, headers=headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Email verified successfully"
    assert data["email"] == "verify.me@example.com"
    assert "verified_at" in data

    # User record updated
    assert user.email == "verify.me@example.com"
    assert user.email_verified_at is not None
    mock_db.commit.assert_awaited()

    # Redis keys deleted
    assert await redis.get(f"email_otp:{user_id}") is None
    assert await redis.get(f"pending_email:{user_id}") is None


@pytest.mark.asyncio
async def test_api_verify_email_invalid_otp_returns_400(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    await redis.set(f"email_otp:{user_id}", "654321")
    await redis.set(f"pending_email:{user_id}", "verify.me@example.com")

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/users/me/email/verify", json={"otp": "111222"}, headers=headers
    )
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "AUTH_INVALID_EMAIL_OTP"

    # Key is preserved for retry
    assert await redis.get(f"email_otp:{user_id}") == "654321"


@pytest.mark.asyncio
async def test_api_verify_email_expired_otp_returns_401(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, _, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    # No OTP in Redis
    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post(
        "/api/v1/users/me/email/verify", json={"otp": "654321"}, headers=headers
    )
    assert response.status_code == 401
    data = response.json()
    assert data["code"] == "AUTH_EMAIL_OTP_EXPIRED"


@pytest.mark.asyncio
async def test_api_changing_existing_email_keeps_old_until_verified(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, _ = users_client
    user_id = uuid.uuid4()
    old_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    user = _make_user(
        user_id=user_id,
        email="old@example.com",
        email_verified_at=old_time,
    )
    mock_db.get.return_value = user

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    # Request new email
    req_resp = await client.post(
        "/api/v1/users/me/email", json={"email": "new@example.com"}, headers=headers
    )
    assert req_resp.status_code == 200

    # Old email is still on user record
    assert user.email == "old@example.com"
    assert user.email_verified_at == old_time

    # Retrieve generated OTP from Redis
    otp = await redis.get(f"email_otp:{user_id}")
    assert otp is not None

    # Verify with OTP
    ver_resp = await client.post(
        "/api/v1/users/me/email/verify", json={"otp": otp}, headers=headers
    )
    assert ver_resp.status_code == 200
    assert user.email == "new@example.com"
    assert user.email_verified_at > old_time


def test_users_router_get_service_factory(test_settings: Settings) -> None:
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    mock_notification = AsyncMock()
    service = _get_service(
        db=mock_db,
        redis_client=mock_redis,
        settings=test_settings,
        notification_service=mock_notification,
    )
    assert isinstance(service, UserService)


def test_users_router_get_notification_service_factory(test_settings: Settings) -> None:
    service = _get_notification_service(settings=test_settings)
    assert isinstance(service, NotificationService)


@pytest.mark.asyncio
async def test_api_malformed_token_subject_returns_401(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, _, _, _ = users_client
    now = datetime.now(UTC)
    bad_token = jwt.encode(
        {
            "sub": "not-a-valid-uuid",
            "role": "customer",
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        test_settings.JWT_ACCESS_SECRET,
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {bad_token}"}

    response = await client.post(
        "/api/v1/users/me/email", json={"email": "test@example.com"}, headers=headers
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_INVALID_TOKEN"

    ver_resp = await client.post(
        "/api/v1/users/me/email/verify", json={"otp": "123456"}, headers=headers
    )
    assert ver_resp.status_code == 401
    assert ver_resp.json()["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.asyncio
async def test_api_verify_email_brute_force_lockout_after_five_failed_attempts(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, redis, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    await redis.set(f"email_otp:{user_id}", "123456")
    await redis.set(f"pending_email:{user_id}", "user@example.com")

    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    # 4 invalid attempts return 400
    for _ in range(4):
        resp = await client.post(
            "/api/v1/users/me/email/verify", json={"otp": "000000"}, headers=headers
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "AUTH_INVALID_EMAIL_OTP"

    # 5th invalid attempt triggers lockout and returns 401
    lockout_resp = await client.post(
        "/api/v1/users/me/email/verify", json={"otp": "000000"}, headers=headers
    )
    assert lockout_resp.status_code == 401
    assert lockout_resp.json()["code"] == "AUTH_EMAIL_OTP_EXPIRED"

    # Keys must be deleted
    assert await redis.get(f"email_otp:{user_id}") is None
    assert await redis.get(f"pending_email:{user_id}") is None
