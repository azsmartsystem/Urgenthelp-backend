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
    InactiveUserError,
    InvalidEmailOTPError,
    UserNotFoundError,
)
from app.modules.notifications.service import NotificationService
from app.modules.users.model import User
from app.modules.users.schemas import UpdateEmailRequest, VerifyEmailRequest
from app.modules.users.service import UserService
from pydantic import ValidationError
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
def user_service(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
    mock_notification_service: AsyncMock,
) -> UserService:
    return UserService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notification_service,
    )


def _make_user(
    user_id: uuid.UUID,
    email: str | None = None,
    is_active: bool = True,
    email_verified_at: datetime | None = None,
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
    from app.modules.users.service import _mask_email

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
