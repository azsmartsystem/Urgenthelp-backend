"""Integration tests for Users API endpoints (Email verification lifecycle)."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import StorageError
from app.core.security import create_access_token
from app.core.storage import MAX_PROFILE_PHOTO_BYTES, InMemoryStorageBackend, StorageBackend
from app.db.redis import get_redis
from app.db.session import get_db
from app.main import app
from app.modules.notifications.email import MockEmailProvider
from app.modules.notifications.service import NotificationService
from app.modules.users.model import User
from app.modules.users.router import (
    _get_notification_service,
    _get_service,
    _get_storage_backend,
)
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
def storage_backend() -> InMemoryStorageBackend:
    return InMemoryStorageBackend()


@pytest.fixture
async def users_client(
    fake_redis_instance: fakeredis.aioredis.FakeRedis,
    test_settings: Settings,
    mock_email_provider: MockEmailProvider,
    storage_backend: InMemoryStorageBackend,
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

    def override_get_notification_service() -> NotificationService:
        return notification_service

    def override_get_storage() -> StorageBackend:
        return storage_backend

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    app.dependency_overrides[get_settings] = override_get_settings
    app.dependency_overrides[_get_notification_service] = override_get_notification_service
    app.dependency_overrides[_get_storage_backend] = override_get_storage

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, mock_db, fake_redis_instance, mock_email_provider

    app.dependency_overrides.clear()


def _make_user(
    user_id: uuid.UUID,
    email: str | None = None,
    email_verified_at: datetime | None = None,
    is_active: bool = True,
    address: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    profile_photo_url: str | None = None,
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
        address=address,
        latitude=latitude,
        longitude=longitude,
        profile_photo_url=profile_photo_url,
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
    custom_backend = InMemoryStorageBackend()
    service = _get_service(
        db=mock_db,
        redis_client=mock_redis,
        settings=test_settings,
        notification_service=mock_notification,
        storage_backend=custom_backend,
    )
    assert isinstance(service, UserService)
    assert service._storage_backend is custom_backend


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


# ─── Profile Management Endpoints Tests ───────────────────────────────────────


def test_users_router_get_storage_backend_factory(test_settings: Settings) -> None:
    mem_settings = test_settings.model_copy(update={"STORAGE_PROVIDER": "memory"})
    backend = _get_storage_backend(settings=mem_settings)
    assert isinstance(backend, InMemoryStorageBackend)
    assert _get_storage_backend(settings=mem_settings) is backend


@pytest.mark.asyncio
async def test_api_profile_endpoints_require_authentication(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, _, _, _ = users_client

    assert (await client.get("/api/v1/users/me")).status_code == 401
    assert (await client.put("/api/v1/users/me", json={"full_name": "Alice"})).status_code == 401
    assert (
        await client.post(
            "/api/v1/users/me/photo",
            files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    ).status_code == 401

    now = datetime.now(UTC)
    bad_sub_token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "role": "customer",
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        test_settings.JWT_ACCESS_SECRET,
        algorithm="HS256",
    )
    bad_headers = {"Authorization": f"Bearer {bad_sub_token}"}
    assert (await client.get("/api/v1/users/me", headers=bad_headers)).status_code == 401
    assert (
        await client.put("/api/v1/users/me", json={"full_name": "Alice"}, headers=bad_headers)
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/users/me/photo",
            files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            headers=bad_headers,
        )
    ).status_code == 401


@pytest.mark.asyncio
async def test_api_get_me_success_and_error_states(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
    storage_backend: InMemoryStorageBackend,
) -> None:
    client, mock_db, _, _ = users_client
    user_id = uuid.uuid4()
    key = f"public/profile-photos/{user_id}/avatar.jpg"
    user = _make_user(
        user_id=user_id,
        email="profile@example.com",
        address="Lekki Phase 1",
        latitude=6.4474,
        longitude=3.4723,
        profile_photo_url=key,
    )
    mock_db.get.return_value = user
    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/api/v1/users/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(user_id)
    assert data["phone"] == "2348011112222"
    assert data["email"] == "profile@example.com"
    assert data["full_name"] == "Verified Test User"
    assert data["role"] == "customer"
    assert data["is_active"] is True
    assert data["is_id_verified"] is False
    assert data["is_phone_verified"] is True
    assert data["trust_score"] == 50.0
    assert data["address"] == "Lekki Phase 1"
    assert data["latitude"] == 6.4474
    assert data["longitude"] == 3.4723
    assert data["profile_photo_url"] == storage_backend.get_public_url(key)

    # User not found -> 404
    mock_db.get.return_value = None
    resp_404 = await client.get("/api/v1/users/me", headers=headers)
    assert resp_404.status_code == 404
    assert resp_404.json()["code"] == "USER_NOT_FOUND"

    # Inactive user -> 403
    mock_db.get.return_value = _make_user(user_id=user_id, is_active=False)
    resp_403 = await client.get("/api/v1/users/me", headers=headers)
    assert resp_403.status_code == 403
    assert resp_403.json()["code"] == "AUTH_USER_INACTIVE"


@pytest.mark.asyncio
async def test_api_put_me_partial_update_and_validation_errors(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
) -> None:
    client, mock_db, _, _ = users_client
    user_id = uuid.uuid4()
    user = _make_user(
        user_id=user_id,
        address="Initial Address",
        latitude=6.5,
        longitude=3.3,
    )
    mock_db.get.return_value = user
    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    # Partial update preserving address & coords
    resp = await client.put(
        "/api/v1/users/me",
        json={"full_name": "Ada Lovelace"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] == "Ada Lovelace"
    assert body["address"] == "Initial Address"
    assert body["latitude"] == 6.5
    assert body["longitude"] == 3.3

    # Paired coordinates update
    resp_coords = await client.put(
        "/api/v1/users/me",
        json={"address": "Victoria Island, Lagos", "latitude": 6.4281, "longitude": 3.4219},
        headers=headers,
    )
    assert resp_coords.status_code == 200
    assert resp_coords.json()["address"] == "Victoria Island, Lagos"
    assert resp_coords.json()["latitude"] == 6.4281
    assert resp_coords.json()["longitude"] == 3.4219

    # Half-specified coordinates -> 422
    resp_half = await client.put(
        "/api/v1/users/me",
        json={"latitude": 6.5},
        headers=headers,
    )
    assert resp_half.status_code == 422

    # Null Island (0.0, 0.0) -> 422
    resp_null_island = await client.put(
        "/api/v1/users/me",
        json={"latitude": 0.0, "longitude": 0.0},
        headers=headers,
    )
    assert resp_null_island.status_code == 422

    # Out-of-bounds coordinates -> 422
    resp_oob = await client.put(
        "/api/v1/users/me",
        json={"latitude": 91.0, "longitude": 3.4},
        headers=headers,
    )
    assert resp_oob.status_code == 422

    # Forbidden field injection -> 422 (no DB mutation)
    for forbidden in (
        {"role": "admin"},
        {"is_id_verified": True},
        {"trust_score": 99.0},
        {"phone": "2348099999999"},
        {"email": "evil@example.com"},
    ):
        resp_forbid = await client.put("/api/v1/users/me", json=forbidden, headers=headers)
        assert resp_forbid.status_code == 422

    # User not found -> 404; inactive -> 403
    mock_db.get.return_value = None
    assert (
        await client.put("/api/v1/users/me", json={"full_name": "Ada"}, headers=headers)
    ).status_code == 404

    mock_db.get.return_value = _make_user(user_id=user_id, is_active=False)
    assert (
        await client.put("/api/v1/users/me", json={"full_name": "Ada"}, headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_api_post_me_photo_valid_and_error_cases(
    users_client: tuple[AsyncClient, AsyncMock, fakeredis.aioredis.FakeRedis, MockEmailProvider],
    test_settings: Settings,
    storage_backend: InMemoryStorageBackend,
) -> None:
    client, mock_db, _, _ = users_client
    user_id = uuid.uuid4()
    old_key = f"public/profile-photos/{user_id}/previous.jpg"
    await storage_backend.upload(old_key, b"old-img", "image/jpeg")

    user = _make_user(user_id=user_id, profile_photo_url=old_key)
    mock_db.get.return_value = user
    token = create_access_token(user_id=user_id, role="customer", settings=test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    # Valid PNG upload (even with mismatched client content_type/filename extension)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    resp = await client.post(
        "/api/v1/users/me/photo",
        files={"file": ("avatar.jpg", png_bytes, "application/octet-stream")},
        headers=headers,
    )
    assert resp.status_code == 200
    assert user.profile_photo_url is not None
    assert user.profile_photo_url.startswith(f"public/profile-photos/{user_id}/")
    assert user.profile_photo_url.endswith(".png")
    assert resp.json()["profile_photo_url"] == storage_backend.get_public_url(
        user.profile_photo_url
    )
    assert storage_backend.get_object(old_key) is None

    # Forged SVG renamed as .jpg -> 415
    svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    resp_svg = await client.post(
        "/api/v1/users/me/photo",
        files={"file": ("evil.jpg", svg_bytes, "image/jpeg")},
        headers=headers,
    )
    assert resp_svg.status_code == 415
    assert resp_svg.json()["code"] == "STORAGE_UNSUPPORTED_MEDIA_TYPE"

    # Oversized > 5MB -> 413
    oversized = b"\xff\xd8\xff\xe0" + b"a" * MAX_PROFILE_PHOTO_BYTES
    resp_big = await client.post(
        "/api/v1/users/me/photo",
        files={"file": ("huge.jpg", oversized, "image/jpeg")},
        headers=headers,
    )
    assert resp_big.status_code == 413
    assert resp_big.json()["code"] == "STORAGE_FILE_TOO_LARGE"

    # Storage backend failure -> 502 and DB unchanged
    current_key = user.profile_photo_url
    with patch.object(
        storage_backend,
        "upload",
        new=AsyncMock(side_effect=StorageError(detail="S3 down")),
    ):
        resp_502 = await client.post(
            "/api/v1/users/me/photo",
            files={"file": ("ok.png", png_bytes, "image/png")},
            headers=headers,
        )
    assert resp_502.status_code == 502
    assert resp_502.json()["code"] == "STORAGE_OPERATION_FAILED"
    assert user.profile_photo_url == current_key

    # User not found -> 404; inactive -> 403
    mock_db.get.return_value = None
    resp_404 = await client.post(
        "/api/v1/users/me/photo",
        files={"file": ("ok.png", png_bytes, "image/png")},
        headers=headers,
    )
    assert resp_404.status_code == 404

    mock_db.get.return_value = _make_user(user_id=user_id, is_active=False)
    resp_403 = await client.post(
        "/api/v1/users/me/photo",
        files={"file": ("ok.png", png_bytes, "image/png")},
        headers=headers,
    )
    assert resp_403.status_code == 403
