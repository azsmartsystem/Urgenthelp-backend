"""Unit tests for UserService and email verification lifecycle."""

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    EmailCooldownError,
    EmailDispatchError,
    EmailOTPExpiredError,
    EmailRateLimitError,
    FileTooLargeError,
    InactiveUserError,
    InvalidEmailOTPError,
    StorageError,
    UnsupportedMediaTypeError,
    UserNotFoundError,
)
from app.core.storage import MAX_PROFILE_PHOTO_BYTES, InMemoryStorageBackend
from app.modules.notifications.service import NotificationService
from app.modules.users.model import User
from app.modules.users.schemas import (
    UpdateEmailRequest,
    UpdateProfileRequest,
    VerifyEmailRequest,
)
from app.modules.users.service import UserService
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.engine import Result
from sqlalchemy.exc import IntegrityError


def _db_returning(value: object) -> Result[tuple[object]]:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = value
    return mock_result


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def mock_notification_service() -> AsyncMock:
    service = AsyncMock(spec=NotificationService)
    service.send_verification_email = AsyncMock()
    return service


@pytest.fixture
def storage_backend() -> InMemoryStorageBackend:
    return InMemoryStorageBackend()


@pytest.fixture
def user_service(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
    mock_notification_service: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> UserService:
    return UserService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notification_service,
        storage_backend=storage_backend,
    )


def _make_user(
    user_id: uuid.UUID,
    email: str | None = None,
    is_active: bool = True,
    email_verified_at: datetime | None = None,
    address: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    profile_photo_url: str | None = None,
) -> User:
    return User(
        id=user_id,
        phone="2348011112222",
        email=email,
        full_name="Test User",
        hashed_password="hashed_password",
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


# ─── request_email_verification Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_email_verification_user_not_found(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = None

    with pytest.raises(UserNotFoundError) as exc_info:
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="new@example.com"),
        )
    assert exc_info.value.code == "USER_NOT_FOUND"
    assert exc_info.value.context["user_id"] == str(user_id)


@pytest.mark.asyncio
async def test_request_email_verification_inactive_user(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    inactive_user = _make_user(user_id=user_id, is_active=False)
    mock_db.get.return_value = inactive_user

    with pytest.raises(InactiveUserError) as exc_info:
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="new@example.com"),
        )
    assert exc_info.value.code == "AUTH_USER_INACTIVE"


@pytest.mark.asyncio
async def test_request_email_verification_email_already_registered_by_other_user(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    other_user = _make_user(user_id=uuid.uuid4(), email="other@example.com")
    mock_db.execute.return_value = _db_returning(other_user)

    with pytest.raises(EmailAlreadyRegisteredError) as exc_info:
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="other@example.com"),
        )
    assert exc_info.value.code == "AUTH_EMAIL_TAKEN"
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_request_email_verification_success_dev_logging(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_notification_service: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    resp = await user_service.request_email_verification(
        user_id=user_id,
        payload=UpdateEmailRequest(email="test@example.com"),
    )
    assert resp.message == "Verification code sent to your email"
    assert resp.email == "test@example.com"

    # Verify state in Redis
    otp = await fake_redis.get(f"email_otp:{user_id}")
    assert otp is not None
    assert len(str(otp)) == 6
    assert str(otp).isdigit()

    pending_email = await fake_redis.get(f"pending_email:{user_id}")
    assert pending_email == "test@example.com"

    cooldown = await fake_redis.get(f"email:cooldown:{user_id}")
    assert cooldown == "1"

    # Verify notification dispatched
    mock_notification_service.send_verification_email.assert_awaited_once_with(
        to="test@example.com",
        otp=otp,
        expire_hours=24,
    )


@pytest.mark.asyncio
async def test_request_email_verification_success_prod_logging(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
    mock_notification_service: AsyncMock,
) -> None:
    prod_settings = settings.model_copy(update={"ENVIRONMENT": "production"})
    service = UserService(
        db=mock_db,
        redis_client=fake_redis,
        settings=prod_settings,
        notification_service=mock_notification_service,
    )

    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    resp = await service.request_email_verification(
        user_id=user_id,
        payload=UpdateEmailRequest(email="prod@example.com"),
    )
    assert resp.email == "prod@example.com"


@pytest.mark.asyncio
async def test_request_email_verification_cooldown_error(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    # First request acquires cooldown
    await user_service.request_email_verification(
        user_id=user_id,
        payload=UpdateEmailRequest(email="user@example.com"),
    )

    # Rapid second request triggers cooldown
    with pytest.raises(EmailCooldownError) as exc_info:
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="user@example.com"),
        )
    assert exc_info.value.code == "AUTH_EMAIL_COOLDOWN"
    assert exc_info.value.status_code == 429
    retry_after = exc_info.value.context["retry_after"]
    assert isinstance(retry_after, int) and retry_after > 0


@pytest.mark.asyncio
async def test_request_email_verification_rate_limit_initial_check(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    # Pre-populate rate limit counter to max
    rate_key = f"email:rate:{user_id}"
    await fake_redis.set(rate_key, str(settings.EMAIL_MAX_REQUESTS_PER_WINDOW), ex=300)

    with pytest.raises(EmailRateLimitError) as exc_info:
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="user@example.com"),
        )
    assert exc_info.value.code == "AUTH_EMAIL_RATE_LIMITED"
    assert exc_info.value.status_code == 429
    retry_after = exc_info.value.context["retry_after"]
    assert isinstance(retry_after, int) and retry_after > 0


@pytest.mark.asyncio
async def test_request_email_verification_rate_limit_after_incr(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    # Simulate race condition: incr returns value exceeding max requests
    with patch.object(
        fake_redis,
        "incr",
        new=AsyncMock(return_value=settings.EMAIL_MAX_REQUESTS_PER_WINDOW + 1),
    ):
        with pytest.raises(EmailRateLimitError) as exc_info:
            await user_service.request_email_verification(
                user_id=user_id,
                payload=UpdateEmailRequest(email="user@example.com"),
            )
        assert exc_info.value.code == "AUTH_EMAIL_RATE_LIMITED"
        assert await fake_redis.get(f"email:cooldown:{user_id}") is None


@pytest.mark.asyncio
async def test_request_email_verification_rate_limit_sets_expire_if_ttl_missing(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    # Key exists without TTL (-1)
    rate_key = f"email:rate:{user_id}"
    await fake_redis.set(rate_key, "1")

    await user_service.request_email_verification(
        user_id=user_id,
        payload=UpdateEmailRequest(email="user@example.com"),
    )
    ttl = await fake_redis.ttl(rate_key)
    assert ttl > 0


@pytest.mark.asyncio
async def test_request_email_verification_dispatch_failure_rolls_back_redis(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_notification_service: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    mock_notification_service.send_verification_email.side_effect = EmailDispatchError(
        detail="Provider network error"
    )

    with pytest.raises(EmailDispatchError):
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="user@example.com"),
        )

    # Verify keys were rolled back
    assert await fake_redis.get(f"email_otp:{user_id}") is None
    assert await fake_redis.get(f"pending_email:{user_id}") is None
    assert await fake_redis.get(f"email:cooldown:{user_id}") is None
    rate_val = await fake_redis.get(f"email:rate:{user_id}")
    assert rate_val is None or int(rate_val) == 0


@pytest.mark.asyncio
async def test_request_email_verification_dispatch_failure_rollback_when_rate_key_empty(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_notification_service: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    async def fail_and_wipe_rate(*args: object, **kwargs: object) -> None:
        await fake_redis.delete(f"email:rate:{user_id}")
        raise RuntimeError("Network down")

    mock_notification_service.send_verification_email.side_effect = fail_and_wipe_rate

    with pytest.raises(RuntimeError):
        await user_service.request_email_verification(
            user_id=user_id,
            payload=UpdateEmailRequest(email="user@example.com"),
        )


# ─── verify_email Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_email_user_not_found(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = None

    with pytest.raises(UserNotFoundError):
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="123456"),
        )


@pytest.mark.asyncio
async def test_verify_email_inactive_user(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    inactive_user = _make_user(user_id=user_id, is_active=False)
    mock_db.get.return_value = inactive_user

    with pytest.raises(InactiveUserError):
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="123456"),
        )


@pytest.mark.asyncio
async def test_verify_email_otp_expired_or_not_requested(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    # Neither otp nor pending email in Redis
    with pytest.raises(EmailOTPExpiredError) as exc_info:
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="123456"),
        )
    assert exc_info.value.code == "AUTH_EMAIL_OTP_EXPIRED"
    assert exc_info.value.status_code == 401

    # Only otp exists, missing pending email
    await fake_redis.set(f"email_otp:{user_id}", "123456")
    with pytest.raises(EmailOTPExpiredError):
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="123456"),
        )


@pytest.mark.asyncio
async def test_verify_email_invalid_otp_mismatch(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    await fake_redis.set(f"email_otp:{user_id}", "123456")
    await fake_redis.set(f"pending_email:{user_id}", "user@example.com")

    with pytest.raises(InvalidEmailOTPError) as exc_info:
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="654321"),
        )
    assert exc_info.value.code == "AUTH_INVALID_EMAIL_OTP"
    assert exc_info.value.status_code == 400
    assert exc_info.value.context["attempts_remaining"] == 4

    # Wrong attempt must not delete/consume the OTP
    assert await fake_redis.get(f"email_otp:{user_id}") == "123456"
    assert await fake_redis.get(f"pending_email:{user_id}") == "user@example.com"


@pytest.mark.asyncio
async def test_verify_email_brute_force_lockout_after_five_failed_attempts(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    await fake_redis.set(f"email_otp:{user_id}", "123456")
    await fake_redis.set(f"pending_email:{user_id}", "user@example.com")

    # 4 failed attempts should raise InvalidEmailOTPError
    for attempt in range(1, 5):
        with pytest.raises(InvalidEmailOTPError) as exc_info:
            await user_service.verify_email(
                user_id=user_id,
                payload=VerifyEmailRequest(otp="000000"),
            )
        assert exc_info.value.context["attempts_remaining"] == 5 - attempt
        assert await fake_redis.get(f"email_otp:{user_id}") == "123456"

    # 5th failed attempt invalidates the OTP and raises EmailOTPExpiredError
    with pytest.raises(EmailOTPExpiredError) as expired_exc_info:
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="000000"),
        )
    assert expired_exc_info.value.code == "AUTH_EMAIL_OTP_EXPIRED"

    # Redis state must be wiped
    assert await fake_redis.get(f"email_otp:{user_id}") is None
    assert await fake_redis.get(f"pending_email:{user_id}") is None
    assert await fake_redis.get(f"email_otp_fails:{user_id}") is None


@pytest.mark.asyncio
async def test_verify_email_concurrent_race_integrity_error_rolls_back(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)
    mock_db.commit.side_effect = IntegrityError(None, None, Exception("unique constraint"))

    await fake_redis.set(f"email_otp:{user_id}", "123456")
    await fake_redis.set(f"pending_email:{user_id}", "user@example.com")

    with pytest.raises(EmailAlreadyRegisteredError) as exc_info:
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="123456"),
        )
    assert exc_info.value.code == "AUTH_EMAIL_TAKEN"
    mock_db.rollback.assert_awaited_once()


def test_update_email_request_schema_normalizes_casing_and_whitespace() -> None:
    req = UpdateEmailRequest(email="  User.Test@Example.COM  ")
    assert req.email == "user.test@example.com"


@pytest.mark.parametrize("invalid_otp", ["12345", "1234567", "abcdef", "12a456"])
def test_verify_email_request_schema_rejects_invalid_otp(invalid_otp: str) -> None:
    with pytest.raises(ValidationError):
        VerifyEmailRequest(otp=invalid_otp)


def test_verify_email_request_schema_accepts_valid_six_digit_otp() -> None:
    req = VerifyEmailRequest(otp="654321")
    assert req.otp == "654321"


def test_mask_email_handles_various_formats() -> None:
    from app.core.email_verification import mask_email as _mask_email

    assert _mask_email("user@example.com") == "u***@example.com"
    assert _mask_email("a@b.com") == "***@b.com"
    assert _mask_email("invalid-email") == "***"


@pytest.mark.asyncio
async def test_email_verification_production_logging(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_notification_service: AsyncMock,
) -> None:
    with patch.dict("os.environ", {"ENVIRONMENT": "production"}):
        prod_settings = Settings()

    prod_service = UserService(
        db=mock_db,
        redis_client=fake_redis,
        settings=prod_settings,
        notification_service=mock_notification_service,
    )
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, email=None)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    # Request in production
    await prod_service.request_email_verification(
        user_id=user_id,
        payload=UpdateEmailRequest(email="prod@example.com"),
    )

    # Verify in production
    otp = await fake_redis.get(f"email_otp:{user_id}")
    assert isinstance(otp, str)
    resp = await prod_service.verify_email(
        user_id=user_id,
        payload=VerifyEmailRequest(otp=otp),
    )
    assert resp.email == "prod@example.com"


@pytest.mark.asyncio
async def test_verify_email_conflict_with_another_user_at_verification(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    await fake_redis.set(f"email_otp:{user_id}", "123456")
    await fake_redis.set(f"pending_email:{user_id}", "taken@example.com")

    other_user = _make_user(user_id=uuid.uuid4(), email="taken@example.com")
    mock_db.execute.return_value = _db_returning(other_user)

    with pytest.raises(EmailAlreadyRegisteredError) as exc_info:
        await user_service.verify_email(
            user_id=user_id,
            payload=VerifyEmailRequest(otp="123456"),
        )
    assert exc_info.value.code == "AUTH_EMAIL_TAKEN"
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_verify_email_success(
    user_service: UserService,
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, email=None, email_verified_at=None)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    await fake_redis.set(f"email_otp:{user_id}", "123456")
    await fake_redis.set(f"pending_email:{user_id}", "verified@example.com")

    resp = await user_service.verify_email(
        user_id=user_id,
        payload=VerifyEmailRequest(otp="123456"),
    )
    assert resp.message == "Email verified successfully"
    assert resp.email == "verified@example.com"
    assert isinstance(resp.verified_at, datetime)

    # Ensure user record updated and committed
    assert user.email == "verified@example.com"
    assert user.email_verified_at is not None
    mock_db.commit.assert_awaited_once()
    mock_db.refresh.assert_awaited_once_with(user)

    # Ensure Redis keys deleted
    assert await fake_redis.get(f"email_otp:{user_id}") is None
    assert await fake_redis.get(f"pending_email:{user_id}") is None


@pytest.mark.asyncio
async def test_verify_email_handles_bytes_from_redis(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    bytes_redis = AsyncMock()
    # Mock Redis returning bytes for stored_otp and pending_email
    bytes_redis.get = AsyncMock(side_effect=[b"999888", b"bytes@example.com"])
    bytes_redis.delete = AsyncMock()

    service = UserService(
        db=mock_db,
        redis_client=bytes_redis,
    )

    resp = await service.verify_email(
        user_id=user_id,
        payload=VerifyEmailRequest(otp="999888"),
    )
    assert resp.email == "bytes@example.com"
    assert resp.message == "Email verified successfully"


@pytest.mark.asyncio
async def test_request_email_verification_uses_dev_static_otp_outside_production(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """When DEV_STATIC_OTP is set in non-prod, email verification uses it and verifies cleanly."""
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="Test User",
        hashed_password="hashed_password",
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(None)

    dev_settings = get_settings().model_copy(
        update={"ENVIRONMENT": "staging", "DEV_STATIC_OTP": "123456"}
    )
    service = UserService(db=mock_db, redis_client=fake_redis, settings=dev_settings)

    await service.request_email_verification(
        user_id=user_id,
        payload=UpdateEmailRequest(email="dev-otp@example.com"),
    )
    assert await fake_redis.get(f"email_otp:{user_id}") == "123456"

    verified = await service.verify_email(
        user_id=user_id,
        payload=VerifyEmailRequest(otp="123456"),
    )
    assert verified.email == "dev-otp@example.com"


# ─── Profile Management Tests ─────────────────────────────────────────────────


class _FakeUploadStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._payload):
            return b""
        if size < 0:
            chunk = self._payload[self._offset :]
        else:
            chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_get_me_returns_profile_and_resolves_photo_url(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    key = f"public/profile-photos/{user_id}/avatar.png"
    user = _make_user(
        user_id=user_id,
        email="me@example.com",
        address="12 Allen Avenue, Ikeja",
        latitude=6.6018,
        longitude=3.3515,
        profile_photo_url=key,
    )
    mock_db.get.return_value = user

    profile = await user_service.get_me(user_id)
    assert profile.id == user_id
    assert profile.email == "me@example.com"
    assert profile.address == "12 Allen Avenue, Ikeja"
    assert profile.latitude == 6.6018
    assert profile.longitude == 3.3515
    assert profile.profile_photo_url == storage_backend.get_public_url(key)


@pytest.mark.asyncio
async def test_get_me_preserves_external_photo_url_and_none(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user_with_external = _make_user(
        user_id=user_id,
        profile_photo_url="https://cdn.external.test/avatar.jpg",
    )
    mock_db.get.return_value = user_with_external
    profile = await user_service.get_me(user_id)
    assert profile.profile_photo_url == "https://cdn.external.test/avatar.jpg"

    user_no_photo = _make_user(user_id=user_id, profile_photo_url=None)
    mock_db.get.return_value = user_no_photo
    profile_none = await user_service.get_me(user_id)
    assert profile_none.profile_photo_url is None

    # Whitespace-only treated as None; leading-slash passed through; invalid key falls back to None
    mock_db.get.return_value = _make_user(user_id=user_id, profile_photo_url="   ")
    assert (await user_service.get_me(user_id)).profile_photo_url is None

    mock_db.get.return_value = _make_user(user_id=user_id, profile_photo_url="/storage/legacy.jpg")
    assert (await user_service.get_me(user_id)).profile_photo_url == "/storage/legacy.jpg"

    mock_db.get.return_value = _make_user(user_id=user_id, profile_photo_url="../malformed.jpg")
    assert (await user_service.get_me(user_id)).profile_photo_url is None


@pytest.mark.asyncio
async def test_get_me_raises_not_found_and_inactive(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = None
    with pytest.raises(UserNotFoundError):
        await user_service.get_me(user_id)

    mock_db.get.return_value = _make_user(user_id=user_id, is_active=False)
    with pytest.raises(InactiveUserError):
        await user_service.get_me(user_id)


@pytest.mark.asyncio
async def test_update_profile_partial_preserves_unset_fields(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(
        user_id=user_id,
        address="Old Address",
        latitude=6.5,
        longitude=3.3,
    )
    mock_db.get.return_value = user

    updated = await user_service.update_profile(
        user_id=user_id,
        payload=UpdateProfileRequest(full_name="  Updated Name  "),
    )
    assert updated.full_name == "Updated Name"
    assert updated.address == "Old Address"
    assert updated.latitude == 6.5
    assert updated.longitude == 3.3
    mock_db.commit.assert_awaited_once()
    mock_db.refresh.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_update_profile_coordinates_clear_and_empty_noop(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(
        user_id=user_id,
        address="Victoria Island",
        latitude=6.4281,
        longitude=3.4219,
    )
    mock_db.get.return_value = user

    # Empty payload makes no DB write
    unchanged = await user_service.update_profile(user_id=user_id, payload=UpdateProfileRequest())
    assert unchanged.address == "Victoria Island"
    mock_db.commit.assert_not_awaited()

    # Clear address and coordinates explicitly with null
    cleared = await user_service.update_profile(
        user_id=user_id,
        payload=UpdateProfileRequest(address=None, latitude=None, longitude=None),
    )
    assert cleared.address is None
    assert cleared.latitude is None
    assert cleared.longitude is None
    mock_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_profile_not_found_and_inactive(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = None
    with pytest.raises(UserNotFoundError):
        await user_service.update_profile(user_id, UpdateProfileRequest(full_name="Alice"))

    mock_db.get.return_value = _make_user(user_id=user_id, is_active=False)
    with pytest.raises(InactiveUserError):
        await user_service.update_profile(user_id, UpdateProfileRequest(full_name="Alice"))


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"role": "admin"},
        {"is_id_verified": True},
        {"is_active": False},
        {"is_phone_verified": True},
        {"trust_score": 100.0},
        {"phone": "2348011112222"},
        {"email": "hacker@example.com"},
        {"full_name": None},
        {"full_name": "   "},
        {"full_name": " A"},
        {"full_name": "a" * 101},
        {"address": "   "},
        {"address": "a" * 501},
        {"latitude": 6.5},
        {"longitude": 3.4},
        {"latitude": None},
        {"latitude": 6.5, "longitude": None},
        {"latitude": 0.0, "longitude": 0.0},
        {"latitude": -90.1, "longitude": 3.4},
        {"latitude": 90.1, "longitude": 3.4},
        {"latitude": 6.5, "longitude": -180.1},
        {"latitude": 6.5, "longitude": 180.1},
        {"latitude": "6.5", "longitude": "3.4"},
        {"latitude": True, "longitude": False},
    ],
)
def test_update_profile_request_rejects_invalid_and_forbidden_inputs(
    bad_payload: dict[str, object],
) -> None:
    adapter: TypeAdapter[UpdateProfileRequest] = TypeAdapter(UpdateProfileRequest)
    with pytest.raises(ValidationError):
        adapter.validate_python(bad_payload)


def test_update_profile_request_accepts_equator_nonzero_and_bounds() -> None:
    req_eq = UpdateProfileRequest(latitude=0.0, longitude=10.5)
    assert req_eq.latitude == 0.0
    assert req_eq.longitude == 10.5

    req_bounds = UpdateProfileRequest(latitude=-90.0, longitude=180.0)
    assert req_bounds.latitude == -90.0
    assert req_bounds.longitude == 180.0


def test_update_profile_request_address_type_normalization() -> None:
    req_home = UpdateProfileRequest.model_validate({"address_type": "Home"})
    assert req_home.address_type == "home"

    req_work = UpdateProfileRequest.model_validate({"address_type": "work"})
    assert req_work.address_type == "work"

    req_others = UpdateProfileRequest.model_validate({"address_type": "Others"})
    assert req_others.address_type == "other"

    req_other = UpdateProfileRequest.model_validate({"address_type": "other"})
    assert req_other.address_type == "other"

    with pytest.raises(ValidationError):
        UpdateProfileRequest.model_validate({"address_type": "invalid_type"})

    with pytest.raises(ValidationError):
        UpdateProfileRequest.model_validate({"address_type": None})


@pytest.mark.asyncio
async def test_update_profile_updates_address_type(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    resp = await user_service.update_profile(
        user_id=user_id,
        payload=UpdateProfileRequest(
            address="12 Broad St, Lagos",
            address_type="work",
        ),
    )
    assert user.address == "12 Broad St, Lagos"
    assert user.address_type == "work"
    assert resp.address == "12 Broad St, Lagos"
    assert resp.address_type == "work"


@pytest.mark.asyncio
async def test_upload_profile_photo_stores_key_and_cleans_up_previous_managed_key(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    old_key = f"public/profile-photos/{user_id}/old-avatar.png"
    await storage_backend.upload(old_key, b"old-bytes", "image/png")

    user = _make_user(user_id=user_id, profile_photo_url=old_key)
    mock_db.get.return_value = user

    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    resp = await user_service.upload_profile_photo(user_id, _FakeUploadStream(jpeg_bytes))

    assert user.profile_photo_url is not None
    assert user.profile_photo_url.startswith(f"public/profile-photos/{user_id}/")
    assert user.profile_photo_url.endswith(".jpg")
    assert user.profile_photo_url != old_key
    assert resp.profile_photo_url == storage_backend.get_public_url(user.profile_photo_url)
    assert storage_backend.get_object(user.profile_photo_url) == (jpeg_bytes, "image/jpeg")
    assert storage_backend.get_object(old_key) is None
    mock_db.commit.assert_awaited_once()
    mock_db.refresh.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_upload_profile_photo_best_effort_old_cleanup_failure_still_succeeds(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    old_key = f"public/profile-photos/{user_id}/old.png"
    user = _make_user(user_id=user_id, profile_photo_url=old_key)
    mock_db.get.return_value = user

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with patch.object(
        storage_backend,
        "delete",
        new=AsyncMock(side_effect=StorageError(detail="delete error")),
    ):
        resp = await user_service.upload_profile_photo(user_id, _FakeUploadStream(png_bytes))

    assert resp.profile_photo_url is not None
    assert user.profile_photo_url is not None
    assert user.profile_photo_url.endswith(".png")


@pytest.mark.asyncio
async def test_upload_profile_photo_ignores_external_url_cleanup(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, profile_photo_url="https://external.example.com/pic.jpg")
    mock_db.get.return_value = user

    webp_bytes = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16
    with patch.object(storage_backend, "delete", new=AsyncMock()) as mock_delete:
        resp = await user_service.upload_profile_photo(user_id, _FakeUploadStream(webp_bytes))

    mock_delete.assert_not_awaited()
    assert resp.profile_photo_url is not None
    assert user.profile_photo_url is not None
    assert user.profile_photo_url.endswith(".webp")


@pytest.mark.asyncio
async def test_upload_profile_photo_storage_failure_leaves_db_unchanged(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, profile_photo_url="public/profile-photos/keep.png")
    mock_db.get.return_value = user

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with patch.object(
        storage_backend,
        "upload",
        new=AsyncMock(side_effect=StorageError(detail="S3 unavailable")),
    ):
        with pytest.raises(StorageError) as exc_info:
            await user_service.upload_profile_photo(user_id, _FakeUploadStream(png_bytes))

    assert exc_info.value.status_code == 502
    assert user.profile_photo_url == "public/profile-photos/keep.png"
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_profile_photo_commit_failure_rolls_back_and_cleans_orphan(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, profile_photo_url=None)
    mock_db.get.return_value = user
    mock_db.commit.side_effect = RuntimeError("DB connection lost")

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with pytest.raises(RuntimeError, match="DB connection lost"):
        await user_service.upload_profile_photo(user_id, _FakeUploadStream(png_bytes))

    mock_db.rollback.assert_awaited_once()
    assert storage_backend._objects == {}


@pytest.mark.asyncio
async def test_upload_profile_photo_commit_and_orphan_cleanup_failure_reraises_db_error(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id, profile_photo_url=None)
    mock_db.get.return_value = user
    mock_db.commit.side_effect = RuntimeError("DB connection lost")

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    with patch.object(
        storage_backend,
        "delete",
        new=AsyncMock(side_effect=StorageError(detail="cleanup failed")),
    ):
        with pytest.raises(RuntimeError, match="DB connection lost"):
            await user_service.upload_profile_photo(user_id, _FakeUploadStream(png_bytes))


@pytest.mark.asyncio
async def test_upload_profile_photo_rejects_oversized_and_forged_before_storage_write(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    mock_db.get.return_value = user

    oversized = b"\xff\xd8\xff\xe0" + b"a" * MAX_PROFILE_PHOTO_BYTES
    with pytest.raises(FileTooLargeError):
        await user_service.upload_profile_photo(user_id, _FakeUploadStream(oversized))

    forged_svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    with pytest.raises(UnsupportedMediaTypeError):
        await user_service.upload_profile_photo(user_id, _FakeUploadStream(forged_svg))

    assert storage_backend._objects == {}
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_profile_photo_not_found_and_inactive(
    user_service: UserService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    mock_db.get.return_value = None
    with pytest.raises(UserNotFoundError):
        await user_service.upload_profile_photo(user_id, _FakeUploadStream(png_bytes))

    mock_db.get.return_value = _make_user(user_id=user_id, is_active=False)
    with pytest.raises(InactiveUserError):
        await user_service.upload_profile_photo(user_id, _FakeUploadStream(png_bytes))


# ─── Identity Verification Upload Tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_identity_verification_accepts_pdf_and_resets_verified_flag(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    from datetime import UTC

    from app.modules.users.model import IdentityVerification

    user_id = uuid.uuid4()
    user = _make_user(user_id=user_id)
    user.is_id_verified = True
    mock_db.get.return_value = user

    async def _fake_refresh(obj: IdentityVerification) -> None:
        obj.id = uuid.uuid4()
        obj.created_at = datetime.now(UTC)

    mock_db.refresh = AsyncMock(side_effect=_fake_refresh)

    pdf_bytes = b"%PDF-1.7\n%mock government id\n"
    res = await user_service.submit_identity_verification(
        user_id=user_id,
        document_type="nin",
        file=_FakeUploadStream(pdf_bytes),
    )

    assert res.user_id == user_id
    assert res.document_type == "nin"
    assert res.status == "pending"
    assert res.document_url.startswith(f"identity-docs/{user_id}/")
    assert res.document_url.endswith(".pdf")
    assert user.is_id_verified is False
    assert res.document_url in storage_backend._objects
    mock_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_identity_verification_rejects_webp_and_oversized(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = _make_user(user_id=user_id)

    webp_bytes = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 16
    with pytest.raises(UnsupportedMediaTypeError):
        await user_service.submit_identity_verification(
            user_id=user_id,
            document_type="drivers_license",
            file=_FakeUploadStream(webp_bytes),
        )

    oversized_pdf = b"%PDF-1.7\n" + b"0" * MAX_PROFILE_PHOTO_BYTES
    with pytest.raises(FileTooLargeError):
        await user_service.submit_identity_verification(
            user_id=user_id,
            document_type="international_passport",
            file=_FakeUploadStream(oversized_pdf),
        )

    assert storage_backend._objects == {}
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_identity_verification_cleans_up_orphan_on_commit_failure(
    user_service: UserService,
    mock_db: AsyncMock,
    storage_backend: InMemoryStorageBackend,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = _make_user(user_id=user_id)
    mock_db.commit.side_effect = RuntimeError("DB failure")

    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    with pytest.raises(RuntimeError, match="DB failure"):
        await user_service.submit_identity_verification(
            user_id=user_id,
            document_type="voters_card",
            file=_FakeUploadStream(jpeg_bytes),
        )

    assert storage_backend._objects == {}
    mock_db.rollback.assert_awaited_once()
