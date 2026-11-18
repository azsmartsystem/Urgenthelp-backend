"""Unit tests for AuthService and security utilities."""

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    EmailDispatchError,
    ForbiddenError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidOTPError,
    InvalidPasswordResetTokenError,
    InvalidTokenError,
    OTPCooldownError,
    OTPExpiredError,
    OTPRateLimitError,
    PasswordResetCooldownError,
    PasswordResetRateLimitError,
    PhoneAlreadyRegisteredError,
    PhoneNotVerifiedError,
    SMSDispatchError,
    UserNotFoundError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp,
    get_current_user,
    hash_password,
    require_role,
    verify_password,
)
from app.core.validators import CanonicalPhone
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    OTPVerifyRequest,
    RegisterRequest,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SendOTPRequest,
)
from app.modules.auth.service import AuthService
from app.modules.users.model import User
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from structlog.testing import capture_logs


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def auth_service(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> AuthService:
    return AuthService(db=mock_db, redis_client=fake_redis, settings=settings)


# ─── Register Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_customer_success(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    # First query checks phone -> None, second checks email -> None
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    payload = RegisterRequest(
        phone="+2348012345678",
        full_name="John Doe",
        password="SecurePassword123!",
        role="customer",
        email="john@example.com",
    )

    response = await auth_service.register(payload)

    assert response.id is not None
    assert response.phone == "2348012345678"
    assert response.full_name == "John Doe"
    assert response.role == "customer"
    assert response.email == "john@example.com"
    assert response.is_phone_verified is False
    assert "verify" in response.message.lower()
    assert mock_db.add.called
    assert mock_db.commit.called

    # Verify OTP was dispatched and stored in Redis
    stored_otp = await fake_redis.get("otp:2348012345678")
    assert stored_otp is not None
    assert len(str(stored_otp)) == 6


@pytest.mark.asyncio
async def test_register_helper_without_email_success(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    payload = RegisterRequest(
        phone="+2348087654321",
        full_name="Jane Helper",
        password="HelperPassword123!",
        role="helper",
        email=None,
    )

    response = await auth_service.register(payload)

    assert response.id is not None
    assert response.phone == "2348087654321"
    assert response.role == "helper"
    assert response.is_phone_verified is False
    assert mock_db.add.called
    assert mock_db.commit.called

    stored_otp = await fake_redis.get("otp:2348087654321")
    assert stored_otp is not None


@pytest.mark.asyncio
async def test_register_send_otp_failure_rolls_back_db_transaction(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    mock_notification_svc = AsyncMock()
    mock_notification_svc.send_otp_sms.side_effect = SMSDispatchError(detail="SMS gateway failure")

    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notification_svc,
    )

    payload = RegisterRequest(
        phone="+2348012345678",
        full_name="John Doe",
        password="SecurePassword123!",
        role="customer",
    )

    with pytest.raises(SMSDispatchError):
        await service.register(payload)

    assert mock_db.rollback.called
    assert not mock_db.commit.called


@pytest.mark.asyncio
async def test_register_db_commit_failure_cleans_up_redis_keys(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result
    mock_db.commit.side_effect = RuntimeError("DB connection dropped on commit")

    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
    )

    payload = RegisterRequest(
        phone="+2348012345678",
        full_name="John Doe",
        password="SecurePassword123!",
        role="customer",
    )

    with pytest.raises(RuntimeError):
        await service.register(payload)

    phone = "2348012345678"
    assert mock_db.rollback.called
    assert await fake_redis.get(f"otp:{phone}") is None
    assert await fake_redis.get(f"otp:cooldown:{phone}") is None
    rate = await fake_redis.get(f"otp:rate:{phone}")
    assert rate is None or int(rate) == 0


@pytest.mark.asyncio
async def test_register_phone_already_exists_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    mock_user = MagicMock(spec=User)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_db.execute.return_value = mock_result

    payload = RegisterRequest(
        phone="+2348012345678",
        full_name="John Doe",
        password="SecurePassword123!",
        role="customer",
    )

    with pytest.raises(PhoneAlreadyRegisteredError):
        await auth_service.register(payload)


@pytest.mark.asyncio
async def test_register_email_already_exists_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    # First query for phone -> None, second query for email -> mock_user
    result_phone = MagicMock()
    result_phone.scalar_one_or_none.return_value = None

    result_email = MagicMock()
    result_email.scalar_one_or_none.return_value = MagicMock(spec=User)

    mock_db.execute.side_effect = [result_phone, result_email]

    payload = RegisterRequest(
        phone="+2348012345678",
        full_name="John Doe",
        password="SecurePassword123!",
        role="customer",
        email="existing@example.com",
    )

    with pytest.raises(EmailAlreadyRegisteredError):
        await auth_service.register(payload)


# ─── Login Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_success(
    auth_service: AuthService,
    mock_db: AsyncMock,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    hashed = hash_password("ValidPassword123!")
    user = User(
        id=user_id,
        phone="+2348012345678",
        full_name="John Doe",
        hashed_password=hashed,
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = LoginRequest(phone="+2348012345678", password="ValidPassword123!")
    tokens = await auth_service.login(payload)

    assert tokens.access_token is not None
    access_payload = decode_token(tokens.access_token, settings.JWT_ACCESS_SECRET)
    assert access_payload.sub == str(user_id)


@pytest.mark.asyncio
async def test_login_unverified_phone_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    hashed = hash_password("ValidPassword123!")
    user = User(
        id=user_id,
        phone="+2348012345678",
        full_name="John Doe",
        hashed_password=hashed,
        role="customer",
        is_active=True,
        is_phone_verified=False,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = LoginRequest(phone="+2348012345678", password="ValidPassword123!")

    with pytest.raises(PhoneNotVerifiedError) as exc_info:
        await auth_service.login(payload)

    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "AUTH_PHONE_NOT_VERIFIED"


@pytest.mark.asyncio
async def test_login_wrong_password_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    hashed = hash_password("ValidPassword123!")
    user = User(
        id=user_id,
        phone="+2348012345678",
        full_name="John Doe",
        hashed_password=hashed,
        role="customer",
        is_active=True,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = LoginRequest(phone="+2348012345678", password="WrongPassword!")

    with pytest.raises(InvalidCredentialsError):
        await auth_service.login(payload)


@pytest.mark.asyncio
async def test_login_user_not_found_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    payload = LoginRequest(phone="+2348012345678", password="Password123!")

    with pytest.raises(InvalidCredentialsError):
        await auth_service.login(payload)


@pytest.mark.asyncio
async def test_login_inactive_user_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    user_id = uuid.uuid4()
    hashed = hash_password("ValidPassword123!")
    user = User(
        id=user_id,
        phone="+2348012345678",
        full_name="John Doe",
        hashed_password=hashed,
        role="customer",
        is_active=False,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = LoginRequest(phone="+2348012345678", password="ValidPassword123!")

    with pytest.raises(InactiveUserError):
        await auth_service.login(payload)


# ─── OTP Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_otp_success(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    phone = CanonicalPhone("2348012345678")
    await auth_service.send_otp(phone)

    stored_otp = await fake_redis.get(f"otp:{phone}")
    assert stored_otp is not None
    assert len(str(stored_otp)) == 6


@pytest.mark.asyncio
async def test_send_otp_production_mode(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    prod_settings = settings.model_copy(update={"ENVIRONMENT": "production"})
    service = AuthService(db=mock_db, redis_client=fake_redis, settings=prod_settings)
    phone = CanonicalPhone("2348012345678")
    await service.send_otp(phone)

    stored_otp = await fake_redis.get(f"otp:{phone}")
    assert stored_otp is not None


@pytest.mark.asyncio
async def test_verify_otp_success(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
    settings: Settings,
) -> None:
    phone = "2348012345678"
    otp = "123456"
    await fake_redis.set(f"otp:{phone}", otp)

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone=phone,
        full_name="John Doe",
        hashed_password="hash",
        role="customer",
        is_active=True,
        is_phone_verified=False,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = OTPVerifyRequest(phone=phone, otp=otp)
    tokens = await auth_service.verify_otp(payload)

    assert tokens.access_token is not None
    assert user.is_phone_verified is True
    assert user.phone_verified_at is not None
    assert mock_db.commit.called
    # Verify OTP was deleted from redis
    assert await fake_redis.get(f"otp:{phone}") is None


@pytest.mark.asyncio
async def test_verify_otp_already_verified_user(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    from datetime import UTC, datetime

    phone = "2348012345678"
    otp = "123456"
    await fake_redis.set(f"otp:{phone}", otp)

    verified_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone=phone,
        full_name="John Doe",
        hashed_password="hash",
        role="customer",
        is_active=True,
        is_phone_verified=True,
        phone_verified_at=verified_at,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = OTPVerifyRequest(phone=phone, otp=otp)
    tokens = await auth_service.verify_otp(payload)

    assert tokens.access_token is not None
    assert user.is_phone_verified is True
    assert user.phone_verified_at == verified_at


@pytest.mark.asyncio
async def test_verify_otp_db_commit_failure_preserves_otp_in_redis(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    phone = "2348012345678"
    otp = "123456"
    await fake_redis.set(f"otp:{phone}", otp)

    user = User(
        id=uuid.uuid4(),
        phone=phone,
        full_name="John Doe",
        hashed_password="hash",
        role="customer",
        is_active=True,
        is_phone_verified=False,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result
    mock_db.commit.side_effect = RuntimeError("DB commit failed")

    payload = OTPVerifyRequest(phone=phone, otp=otp)
    with pytest.raises(RuntimeError):
        await auth_service.verify_otp(payload)

    # OTP must still exist in Redis so the user can retry
    assert await fake_redis.get(f"otp:{phone}") == otp


@pytest.mark.asyncio
async def test_verify_otp_expired_raises(
    auth_service: AuthService,
) -> None:
    payload = OTPVerifyRequest(phone="2348012345678", otp="123456")
    with pytest.raises(OTPExpiredError):
        await auth_service.verify_otp(payload)


@pytest.mark.asyncio
async def test_verify_otp_invalid_raises(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    phone = "2348012345678"
    await fake_redis.set(f"otp:{phone}", "123456")

    payload = OTPVerifyRequest(phone=phone, otp="654321")
    with pytest.raises(InvalidOTPError):
        await auth_service.verify_otp(payload)


@pytest.mark.asyncio
async def test_verify_otp_user_not_found_raises(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    phone = "2348012345678"
    await fake_redis.set(f"otp:{phone}", "123456")

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    payload = OTPVerifyRequest(phone=phone, otp="123456")
    with pytest.raises(UserNotFoundError):
        await auth_service.verify_otp(payload)


@pytest.mark.asyncio
async def test_verify_otp_inactive_user_raises(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    phone = "2348012345678"
    await fake_redis.set(f"otp:{phone}", "123456")

    user = User(
        id=uuid.uuid4(),
        phone=phone,
        full_name="Inactive User",
        hashed_password="hash",
        role="customer",
        is_active=False,
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = OTPVerifyRequest(phone=phone, otp="123456")
    with pytest.raises(InactiveUserError):
        await auth_service.verify_otp(payload)


# ─── Phone Canonicalisation Tests ────────────────────────────────────────────
#
# send_otp writes the Redis key `otp:<canonical>` and verify_otp reads it back.
# If either end ever sees a raw "0815… " the user gets a 401 they cannot
# diagnose — the OTP was issued, it just is not under the name verify_otp looks
# for. CanonicalPhone is what turns that into a type error at build time, so the
# tests below drive both methods through the exact path the router uses (schema
# normalises, then the value is narrowed) rather than hand-writing an
# already-canonical string. A test that used one format at both ends would still
# pass after that regression.

CANONICAL_PHONE = "2348153551975"

# Every accepted spelling of the same subscriber. Order is irrelevant; the point
# is that the cross-product below covers each spelling at *both* ends.
EQUIVALENT_FORMATS = [
    "08153551975",
    "2348153551975",
    "+2348153551975",
    "0815 355 1975",
    "+234-815-355-1975",
]


async def _send_otp_like_router(service: AuthService, raw_phone: str) -> None:
    """Mirror app/modules/auth/router.py::send_otp — normalise, then narrow."""
    payload = SendOTPRequest(phone=raw_phone)
    await service.send_otp(CanonicalPhone(payload.phone))


def _active_user(phone: str = CANONICAL_PHONE) -> User:
    return User(
        id=uuid.uuid4(),
        phone=phone,
        full_name="Canonical User",
        hashed_password="hash",
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )


def _db_returning(user: User | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("send_format", EQUIVALENT_FORMATS)
@pytest.mark.parametrize("verify_format", EQUIVALENT_FORMATS)
async def test_send_otp_then_verify_otp_round_trips_across_every_input_format(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
    send_format: str,
    verify_format: str,
) -> None:
    """The invariant the CanonicalPhone refactor exists to guarantee.

    Whichever spelling the user types to request a code, and whichever spelling
    they type to redeem it, both ends must agree on one Redis key.
    """
    mock_db.execute.return_value = _db_returning(_active_user())

    await _send_otp_like_router(auth_service, send_format)
    stored_otp = await fake_redis.get(f"otp:{CANONICAL_PHONE}")
    assert stored_otp is not None, f"send_otp({send_format!r}) did not write the canonical key"

    tokens = await auth_service.verify_otp(
        OTPVerifyRequest(phone=verify_format, otp=str(stored_otp))
    )

    assert tokens.access_token is not None
    assert await fake_redis.get(f"otp:{CANONICAL_PHONE}") is None


@pytest.mark.asyncio
async def test_send_otp_writes_the_canonical_key_and_no_other(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """No alias is written for the raw input — one subscriber, one key."""
    await _send_otp_like_router(auth_service, "0815 355 1975")

    assert await fake_redis.get("otp:0815 355 1975") is None
    assert await fake_redis.get("otp:2348153551975") is not None


@pytest.mark.asyncio
async def test_send_otp_sets_ttl_from_settings(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    """The code must expire exactly OTP_EXPIRE_MINUTES after it is issued."""
    await _send_otp_like_router(auth_service, "08153551975")

    assert await fake_redis.ttl(f"otp:{CANONICAL_PHONE}") == settings.OTP_EXPIRE_MINUTES * 60


@pytest.mark.asyncio
async def test_send_otp_value_is_a_six_digit_number(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """secrets.randbelow(900000) + 100000 must stay inside [100000, 999999].

    Checked over repeated draws because the failure mode is a boundary
    off-by-one, not a single unlucky call.
    """
    for _ in range(25):
        await fake_redis.flushdb()
        await _send_otp_like_router(auth_service, "08153551975")
        stored = await fake_redis.get(f"otp:{CANONICAL_PHONE}")
        assert stored is not None
        assert str(stored).isascii() and str(stored).isdigit()
        assert 100000 <= int(str(stored)) <= 999999


@pytest.mark.asyncio
async def test_send_otp_replaces_a_previously_issued_code(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Requesting a second code invalidates the first."""
    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "000000")

    await _send_otp_like_router(auth_service, "08153551975")

    stored = await fake_redis.get(f"otp:{CANONICAL_PHONE}")
    assert stored is not None
    assert str(stored) != "000000"


@pytest.mark.asyncio
async def test_send_otp_in_development_logs_the_code_for_the_operator(
    auth_service: AuthService,
    settings: Settings,
) -> None:
    """Dev mode has no SMS gateway, so the code is surfaced in the log."""
    assert settings.is_production is False

    with capture_logs() as captured:
        await _send_otp_like_router(auth_service, "08153551975")

    generated = [entry for entry in captured if entry.get("event") == "otp_generated_dev"]
    assert len(generated) == 1
    assert generated[0]["phone"] == CANONICAL_PHONE
    assert generated[0]["ttl"] == settings.OTP_EXPIRE_MINUTES * 60
    assert "otp" in generated[0]


@pytest.mark.asyncio
async def test_send_otp_in_production_never_logs_the_code(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    """The production branch must log neither the code nor the full number.

    This is the branch that keeps OTPs out of log aggregators; the pre-existing
    production test only asserted the Redis key, so a regression here would have
    been invisible.
    """
    prod_settings = settings.model_copy(update={"ENVIRONMENT": "production"})
    service = AuthService(db=mock_db, redis_client=fake_redis, settings=prod_settings)

    with capture_logs() as captured:
        await _send_otp_like_router(service, "08153551975")

    dispatched = [entry for entry in captured if entry.get("event") == "otp_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["phone"] == f"...{CANONICAL_PHONE[-4:]}"
    assert "otp" not in dispatched[0]
    assert all("otp_generated_dev" != entry.get("event") for entry in captured)


@pytest.mark.asyncio
async def test_verify_otp_looks_the_user_up_by_canonical_phone(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    """The DB comparison uses the canonical value, not whatever was typed."""
    mock_db.execute.return_value = _db_returning(_active_user())
    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "123456")

    await auth_service.verify_otp(OTPVerifyRequest(phone="+234 815 355 1975", otp="123456"))

    statement = mock_db.execute.call_args[0][0]
    assert list(dict(statement.compile().params).values()) == [CANONICAL_PHONE]


@pytest.mark.asyncio
async def test_verify_otp_keeps_the_code_when_the_attempt_is_wrong(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """A mistyped code must not consume the user's real one.

    Also guards the ordering inside verify_otp: the delete happens only after
    the code matches.
    """
    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "123456")

    with pytest.raises(InvalidOTPError):
        await auth_service.verify_otp(OTPVerifyRequest(phone="+234-815-355-1975", otp="654321"))

    assert await fake_redis.get(f"otp:{CANONICAL_PHONE}") == "123456"


@pytest.mark.asyncio
async def test_verify_otp_keeps_a_correct_code_when_the_account_is_missing(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    """A correct code survives a failure that has nothing to do with the code.

    Guards the ordering inside verify_otp: the Redis key is deleted only after
    the user lookup and the active check both pass. Deleting earlier destroyed a
    valid code for an inactive or unknown account, and the user had no way to
    tell that their code — not their typing — was the problem.
    """
    mock_db.execute.return_value = _db_returning(None)
    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "123456")

    with pytest.raises(UserNotFoundError):
        await auth_service.verify_otp(OTPVerifyRequest(phone="08153551975", otp="123456"))

    assert await fake_redis.get(f"otp:{CANONICAL_PHONE}") == "123456"


@pytest.mark.asyncio
async def test_verify_otp_keeps_a_correct_code_when_the_account_is_inactive(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    """An inactive account must not destroy a code the user entered correctly."""
    inactive = _db_returning(MagicMock(is_active=False))
    mock_db.execute.return_value = inactive
    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "123456")

    with pytest.raises(InactiveUserError):
        await auth_service.verify_otp(OTPVerifyRequest(phone="08153551975", otp="123456"))

    assert await fake_redis.get(f"otp:{CANONICAL_PHONE}") == "123456"


@pytest.mark.asyncio
async def test_verify_otp_consumes_the_code_on_success(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    """A redeemed code is single-use — otherwise the same code would replay."""
    active = MagicMock(is_active=True, id=uuid.uuid4(), role="customer")
    mock_db.execute.return_value = _db_returning(active)
    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "123456")

    await auth_service.verify_otp(OTPVerifyRequest(phone="08153551975", otp="123456"))

    assert await fake_redis.get(f"otp:{CANONICAL_PHONE}") is None


@pytest.mark.asyncio
async def test_otp_failures_expose_only_the_last_four_phone_digits(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    """A phone number must never reach logs or error payloads in full."""
    with pytest.raises(OTPExpiredError) as expired:
        await auth_service.verify_otp(OTPVerifyRequest(phone="0815 355 1975", otp="123456"))
    assert expired.value.context == {"phone": "1975"}

    await fake_redis.set(f"otp:{CANONICAL_PHONE}", "123456")
    with pytest.raises(InvalidOTPError) as wrong_code:
        await auth_service.verify_otp(OTPVerifyRequest(phone="+234-815-355-1975", otp="654321"))
    assert wrong_code.value.context == {"phone": "1975"}

    mock_db.execute.return_value = _db_returning(None)
    with pytest.raises(UserNotFoundError) as no_user:
        await auth_service.verify_otp(OTPVerifyRequest(phone="2348153551975", otp="123456"))
    assert no_user.value.context == {"phone": "1975"}


@pytest.mark.asyncio
async def test_register_stores_and_looks_up_the_canonical_phone(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    """Registration normalises once, then uses that value for both the
    uniqueness check and the row it inserts."""
    mock_db.execute.return_value = _db_returning(None)

    await auth_service.register(
        RegisterRequest(
            phone="0815 355 1975",
            full_name="Canonical User",
            password="SecurePassword1!",
            role="customer",
        )
    )

    stored_user = mock_db.add.call_args[0][0]
    assert isinstance(stored_user, User)
    assert stored_user.phone == CANONICAL_PHONE
    assert stored_user.is_phone_verified is False

    statement = mock_db.execute.call_args_list[0][0][0]
    assert list(dict(statement.compile().params).values()) == [CANONICAL_PHONE]


@pytest.mark.asyncio
async def test_login_looks_up_the_canonical_phone(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    """A user registered as '0815…' can log in as '+234-…' — same DB value."""
    user = User(
        id=uuid.uuid4(),
        phone=CANONICAL_PHONE,
        full_name="John Doe",
        hashed_password=hash_password("ValidPassword123!"),
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )
    mock_db.execute.return_value = _db_returning(user)

    await auth_service.login(LoginRequest(phone="+234-815-355-1975", password="ValidPassword123!"))

    statement = mock_db.execute.call_args[0][0]
    assert list(dict(statement.compile().params).values()) == [CANONICAL_PHONE]


# ─── Refresh Token Tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_token_success(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="+2348012345678",
        full_name="John Doe",
        hashed_password="hash",
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    refresh_token = create_refresh_token(user_id, "customer", settings)

    tokens = await auth_service.refresh(refresh_token)

    assert tokens.access_token is not None
    assert tokens.refresh_token is not None
    # Old refresh token is blacklisted
    assert await fake_redis.get(f"blacklist:{refresh_token}") == "1"


@pytest.mark.asyncio
async def test_refresh_with_access_token_raises(
    auth_service: AuthService,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime, timedelta

    from jose import jwt

    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    # Encode with refresh secret but type="access" to test the type check branch
    token_with_wrong_type = jwt.encode(
        {
            "sub": str(user_id),
            "role": "customer",
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        settings.JWT_REFRESH_SECRET,
        algorithm="HS256",
    )

    with pytest.raises(InvalidTokenError):
        await auth_service.refresh(token_with_wrong_type)


@pytest.mark.asyncio
async def test_refresh_blacklisted_token_raises(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    refresh_token = create_refresh_token(user_id, "customer", settings)
    await fake_redis.set(f"blacklist:{refresh_token}", "1")

    with pytest.raises(InvalidTokenError):
        await auth_service.refresh(refresh_token)


@pytest.mark.asyncio
async def test_refresh_user_not_found_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    refresh_token = create_refresh_token(user_id, "customer", settings)
    mock_db.get.return_value = None

    with pytest.raises(UserNotFoundError):
        await auth_service.refresh(refresh_token)


@pytest.mark.asyncio
async def test_refresh_inactive_user_raises(
    auth_service: AuthService,
    mock_db: AsyncMock,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="+2348012345678",
        full_name="Inactive User",
        hashed_password="hash",
        role="customer",
        is_active=False,
    )
    mock_db.get.return_value = user

    refresh_token = create_refresh_token(user_id, "customer", settings)

    with pytest.raises(InactiveUserError):
        await auth_service.refresh(refresh_token)


@pytest.mark.asyncio
async def test_logout_blacklists_refresh_token_and_blocks_refresh(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    refresh_token = create_refresh_token(user_id, "customer", settings)

    await auth_service.logout(refresh_token)

    assert await fake_redis.get(f"blacklist:{refresh_token}") == "1"
    ttl = await fake_redis.ttl(f"blacklist:{refresh_token}")
    assert 0 < ttl <= settings.JWT_REFRESH_EXPIRE_DAYS * 86400

    with pytest.raises(InvalidTokenError):
        await auth_service.refresh(refresh_token)


@pytest.mark.asyncio
async def test_logout_is_idempotent_for_already_blacklisted_token(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    refresh_token = create_refresh_token(user_id, "customer", settings)

    await auth_service.logout(refresh_token)
    await auth_service.logout(refresh_token)

    assert await fake_redis.get(f"blacklist:{refresh_token}") == "1"


@pytest.mark.asyncio
async def test_logout_is_idempotent_for_expired_refresh_token(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime, timedelta

    user_id = uuid.uuid4()
    expired_iat = datetime.now(UTC) - timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS + 1)
    expired_refresh = create_refresh_token(user_id, "customer", settings, issued_at=expired_iat)

    await auth_service.logout(expired_refresh)

    assert await fake_redis.get(f"blacklist:{expired_refresh}") is None


@pytest.mark.asyncio
async def test_logout_invalid_token_raises(auth_service: AuthService) -> None:
    with pytest.raises(InvalidTokenError):
        await auth_service.logout("invalid.jwt.token")


@pytest.mark.asyncio
async def test_logout_with_access_token_raises(
    auth_service: AuthService,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime, timedelta

    from jose import jwt

    user_id = uuid.uuid4()
    access_token = create_access_token(user_id, "customer", settings)
    with pytest.raises(InvalidTokenError):
        await auth_service.logout(access_token)

    now = datetime.now(UTC)
    token_with_wrong_type = jwt.encode(
        {
            "sub": str(user_id),
            "role": "customer",
            "type": "access",
            "iat": now,
            "exp": now + timedelta(days=1),
        },
        settings.JWT_REFRESH_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        await auth_service.logout(token_with_wrong_type)


# ─── Security Utilities & Dependencies Tests ─────────────────────────────────


def test_password_hashing_and_verification() -> None:
    pwd = "MySecretPassword123!"
    hashed = hash_password(pwd)
    assert hashed != pwd
    assert verify_password(pwd, hashed) is True
    assert verify_password("WrongPassword", hashed) is False


def test_decode_invalid_token_raises(settings: Settings) -> None:
    with pytest.raises(InvalidTokenError):
        decode_token("invalid.jwt.token", settings.JWT_ACCESS_SECRET)


def test_get_current_user_valid(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, "customer", settings)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    payload = get_current_user(credentials=creds, settings=settings)
    assert payload.sub == str(user_id)
    assert payload.role == "customer"


def test_get_current_user_missing_credentials(settings: Settings) -> None:
    with pytest.raises(InvalidTokenError):
        get_current_user(credentials=None, settings=settings)


def test_get_current_user_with_refresh_token_raises(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token = create_refresh_token(user_id, "customer", settings)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(InvalidTokenError):
        get_current_user(credentials=creds, settings=settings)


def test_require_role_success(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, "admin", settings)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    payload = get_current_user(credentials=creds, settings=settings)

    check_admin = require_role("admin")
    result = check_admin(payload)
    assert result.role == "admin"


def test_require_role_forbidden_raises(settings: Settings) -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, "customer", settings)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    payload = get_current_user(credentials=creds, settings=settings)

    check_admin = require_role("admin")
    with pytest.raises(ForbiddenError):
        check_admin(payload)


@pytest.mark.asyncio
async def test_send_otp_failure_rolls_back_redis_key(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    mock_notification_svc = AsyncMock()
    mock_notification_svc.send_otp_sms.side_effect = SMSDispatchError(detail="SMS gateway failure")

    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notification_svc,
    )

    phone = CanonicalPhone("2348153551975")
    with pytest.raises(SMSDispatchError):
        await service.send_otp(phone)

    # Redis key must be purged so no orphaned un-sent OTP remains
    assert await fake_redis.get(f"otp:{phone}") is None


@pytest.mark.asyncio
async def test_send_otp_calls_notification_service(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    mock_notification_svc = AsyncMock()

    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notification_svc,
    )

    phone = CanonicalPhone("2348153551975")
    await service.send_otp(phone)

    mock_notification_svc.send_otp_sms.assert_awaited_once()
    stored_otp = await fake_redis.get(f"otp:{phone}")
    assert stored_otp is not None


@pytest.mark.asyncio
async def test_send_otp_cooldown_raises_when_requested_too_soon(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    phone = CanonicalPhone("2348153551975")
    await auth_service.send_otp(phone)

    with pytest.raises(OTPCooldownError) as exc_info:
        await auth_service.send_otp(phone)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_OTP_COOLDOWN"
    assert "retry_after" in exc_info.value.context


@pytest.mark.asyncio
async def test_send_otp_cooldown_allows_request_after_expiry(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    phone = CanonicalPhone("2348153551975")
    await auth_service.send_otp(phone)

    await fake_redis.delete(f"otp:cooldown:{phone}")

    await auth_service.send_otp(phone)
    assert await fake_redis.get(f"otp:{phone}") is not None


@pytest.mark.asyncio
async def test_send_otp_rate_limit_raises_when_exceeding_max_requests(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    phone = CanonicalPhone("2348153551975")

    for _ in range(settings.OTP_MAX_REQUESTS_PER_WINDOW):
        await fake_redis.delete(f"otp:cooldown:{phone}")
        await auth_service.send_otp(phone)

    await fake_redis.delete(f"otp:cooldown:{phone}")
    with pytest.raises(OTPRateLimitError) as exc_info:
        await auth_service.send_otp(phone)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_OTP_RATE_LIMITED"
    assert "retry_after" in exc_info.value.context


@pytest.mark.asyncio
async def test_send_otp_rate_limit_not_masked_by_cooldown_within_60s(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    phone = CanonicalPhone("2348153551975")

    for _ in range(settings.OTP_MAX_REQUESTS_PER_WINDOW):
        await fake_redis.delete(f"otp:cooldown:{phone}")
        await auth_service.send_otp(phone)

    # Rate limit has been reached. Calling send_otp without deleting cooldown
    # must trigger OTPRateLimitError (not masked by OTPCooldownError).
    with pytest.raises(OTPRateLimitError) as exc_info:
        await auth_service.send_otp(phone)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_OTP_RATE_LIMITED"


@pytest.mark.asyncio
async def test_send_otp_rate_limit_exceeded_post_increment_deletes_cooldown(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from unittest.mock import AsyncMock, patch

    phone = CanonicalPhone("2348153551975")
    with patch.object(
        fake_redis,
        "incr",
        new_callable=AsyncMock,
        return_value=settings.OTP_MAX_REQUESTS_PER_WINDOW + 1,
    ):
        with pytest.raises(OTPRateLimitError) as exc_info:
            await auth_service.send_otp(phone)

        assert exc_info.value.code == "AUTH_OTP_RATE_LIMITED"
        assert await fake_redis.get(f"otp:cooldown:{phone}") is None


@pytest.mark.asyncio
async def test_send_otp_failure_decrements_rate_limit_counter(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    mock_notification_svc = AsyncMock()
    mock_notification_svc.send_otp_sms.side_effect = SMSDispatchError(
        detail="SMS network error", context={}
    )

    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notification_svc,
    )

    phone = CanonicalPhone("2348153551975")
    with pytest.raises(SMSDispatchError):
        await service.send_otp(phone)

    counter = await fake_redis.get(f"otp:rate:{phone}")
    assert counter is None or int(counter) == 0
    assert await fake_redis.get(f"otp:cooldown:{phone}") is None


# ─── Tiered Password Recovery Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_forgot_password_tier1_verified_email_with_phone_input(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        email="verified@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Verified User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)

    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    assert "instructions have been sent" in response.message
    mock_notif.send_password_reset_email.assert_awaited_once()
    mock_notif.send_password_reset_sms.assert_not_called()

    email_call_kwargs = mock_notif.send_password_reset_email.call_args.kwargs
    assert email_call_kwargs["to"] == "verified@example.com"
    token = email_call_kwargs["token"]
    assert token is not None

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    stored_user_id = await fake_redis.get(f"pwd_reset:token:{token_hash}")
    assert stored_user_id == str(user_id)
    ttl = await fake_redis.ttl(f"pwd_reset:token:{token_hash}")
    assert ttl == settings.PASSWORD_RESET_EXPIRE_MINUTES * 60


@pytest.mark.asyncio
async def test_forgot_password_tier1_verified_email_with_email_input(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        email="verified@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Verified User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)

    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(email="verified@example.com")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    mock_notif.send_password_reset_email.assert_awaited_once()
    mock_notif.send_password_reset_sms.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_tier2_unverified_email_sends_sms_otp(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        email="unverified@example.com",
        email_verified_at=None,
        full_name="Unverified Email User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)

    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    mock_notif.send_password_reset_sms.assert_awaited_once()
    mock_notif.send_password_reset_email.assert_not_called()

    sms_kwargs = mock_notif.send_password_reset_sms.call_args.kwargs
    assert sms_kwargs["to"] == "2348153551975"
    otp = sms_kwargs["otp"]
    assert len(otp) == 6 and otp.isdigit()

    otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()
    stored_user_id = await fake_redis.get(f"pwd_reset:token:{otp_hash}")
    assert stored_user_id == str(user_id)


@pytest.mark.asyncio
async def test_forgot_password_non_existent_account_anti_enumeration(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    mock_db.execute.return_value = _db_returning(None)
    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    assert "instructions have been sent" in response.message
    mock_notif.send_password_reset_email.assert_not_called()
    mock_notif.send_password_reset_sms.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_inactive_user_anti_enumeration(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user = User(
        id=uuid.uuid4(),
        phone="2348153551975",
        email="inactive@example.com",
        full_name="Inactive User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=False,
    )
    mock_db.execute.return_value = _db_returning(user)
    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    mock_notif.send_password_reset_email.assert_not_called()
    mock_notif.send_password_reset_sms.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_tier3_no_email_no_phone(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user = User(
        id=uuid.uuid4(),
        phone="",
        email=None,
        email_verified_at=None,
        full_name="Unreachable User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)
    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    mock_notif.send_password_reset_email.assert_not_called()
    mock_notif.send_password_reset_sms.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_mismatched_phone_and_email_returns_anti_enumeration(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime

    user = User(
        id=uuid.uuid4(),
        phone="2348153551975",
        email="real@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Real User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)
    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975", email="wrong@example.com")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    mock_notif.send_password_reset_email.assert_not_called()


@pytest.mark.asyncio
async def test_forgot_password_matching_phone_and_email_sends_email(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime

    user = User(
        id=uuid.uuid4(),
        phone="2348153551975",
        email="match@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Match User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)
    mock_notif = AsyncMock()
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975", email="match@example.com")
    response = await service.forgot_password(payload)

    assert isinstance(response, ForgotPasswordResponse)
    mock_notif.send_password_reset_email.assert_awaited_once()


@pytest.mark.asyncio
async def test_forgot_password_cooldown_raises_429(
    auth_service: AuthService,
    mock_db: AsyncMock,
) -> None:
    mock_db.execute.return_value = _db_returning(None)
    payload = ForgotPasswordRequest(phone="08153551975")

    await auth_service.forgot_password(payload)

    with pytest.raises(PasswordResetCooldownError) as exc_info:
        await auth_service.forgot_password(payload)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_PASSWORD_RESET_COOLDOWN"
    assert "retry_after" in exc_info.value.context


@pytest.mark.asyncio
async def test_forgot_password_rate_limit_exceeded_raises_429(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    mock_db.execute.return_value = _db_returning(None)
    canonical = "2348153551975"
    payload = ForgotPasswordRequest(phone=canonical)

    for _ in range(3):
        await fake_redis.delete(f"pwd_reset:cooldown:{canonical}")
        await auth_service.forgot_password(payload)

    await fake_redis.delete(f"pwd_reset:cooldown:{canonical}")
    with pytest.raises(PasswordResetRateLimitError) as exc_info:
        await auth_service.forgot_password(payload)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_PASSWORD_RESET_RATE_LIMITED"
    assert "retry_after" in exc_info.value.context


@pytest.mark.asyncio
async def test_forgot_password_rate_limit_not_masked_by_cooldown(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    mock_db.execute.return_value = _db_returning(None)
    canonical = "2348153551975"
    payload = ForgotPasswordRequest(phone=canonical)

    for _ in range(3):
        await fake_redis.delete(f"pwd_reset:cooldown:{canonical}")
        await auth_service.forgot_password(payload)

    with pytest.raises(PasswordResetRateLimitError) as exc_info:
        await auth_service.forgot_password(payload)

    assert exc_info.value.code == "AUTH_PASSWORD_RESET_RATE_LIMITED"


@pytest.mark.asyncio
async def test_forgot_password_rate_limit_exceeded_post_increment_deletes_cooldown(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    from unittest.mock import AsyncMock, patch

    phone = "2348153551975"
    payload = ForgotPasswordRequest(phone=phone)
    with patch.object(
        fake_redis,
        "incr",
        new_callable=AsyncMock,
        return_value=4,
    ):
        with pytest.raises(PasswordResetRateLimitError) as exc_info:
            await auth_service.forgot_password(payload)

        assert exc_info.value.code == "AUTH_PASSWORD_RESET_RATE_LIMITED"
        assert await fake_redis.get(f"pwd_reset:cooldown:{phone}") is None


@pytest.mark.asyncio
async def test_forgot_password_ip_rate_limit_exceeded_raises_429(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    mock_db: AsyncMock,
) -> None:
    mock_db.execute.return_value = _db_returning(None)
    ip = "192.168.1.100"

    for i in range(10):
        payload = ForgotPasswordRequest(phone=f"0815355197{i}")
        await fake_redis.delete(f"pwd_reset:cooldown:234815355197{i}")
        await auth_service.forgot_password(payload, ip=ip)

    payload_11 = ForgotPasswordRequest(phone="08153551980")
    with pytest.raises(PasswordResetRateLimitError) as exc_info:
        await auth_service.forgot_password(payload_11, ip=ip)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_PASSWORD_RESET_RATE_LIMITED"
    assert exc_info.value.context["ip"] == ip


@pytest.mark.asyncio
async def test_forgot_password_email_dispatch_failure_rolls_back_redis(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime

    user = User(
        id=uuid.uuid4(),
        phone="2348153551975",
        email="verified@example.com",
        email_verified_at=datetime.now(UTC),
        full_name="Verified User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)

    mock_notif = AsyncMock()
    mock_notif.send_password_reset_email.side_effect = EmailDispatchError(detail="SMTP outage")
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(email="verified@example.com")
    with pytest.raises(EmailDispatchError):
        await service.forgot_password(payload)

    assert await fake_redis.get("pwd_reset:cooldown:verified@example.com") is None
    rate = await fake_redis.get("pwd_reset:rate:verified@example.com")
    assert rate is None or int(rate) == 0


@pytest.mark.asyncio
async def test_forgot_password_sms_dispatch_failure_rolls_back_redis(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user = User(
        id=uuid.uuid4(),
        phone="2348153551975",
        email=None,
        email_verified_at=None,
        full_name="User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.execute.return_value = _db_returning(user)

    mock_notif = AsyncMock()
    mock_notif.send_password_reset_sms.side_effect = SMSDispatchError(detail="SMS gateway failure")
    service = AuthService(
        db=mock_db,
        redis_client=fake_redis,
        settings=settings,
        notification_service=mock_notif,
    )

    payload = ForgotPasswordRequest(phone="08153551975")
    with pytest.raises(SMSDispatchError):
        await service.forgot_password(payload)

    canonical = "2348153551975"
    assert await fake_redis.get(f"pwd_reset:cooldown:{canonical}") is None
    rate = await fake_redis.get(f"pwd_reset:rate:{canonical}")
    assert rate is None or int(rate) == 0


@pytest.mark.asyncio
async def test_reset_password_success_tier1_token(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    old_hash = hash_password("OldPassword123!")
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="Reset User",
        hashed_password=old_hash,
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    raw_token = "secure_random_reset_token_xyz"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)

    payload = ResetPasswordRequest(token=raw_token, new_password="NewSecurePassword123!")
    response = await service.reset_password(payload)

    assert isinstance(response, ResetPasswordResponse)
    assert response.message == "Password has been reset successfully."
    assert verify_password("NewSecurePassword123!", user.hashed_password) is True
    assert mock_db.commit.called

    assert await fake_redis.get(f"pwd_reset:token:{token_hash}") is None

    revoked_ts = await fake_redis.get(f"auth:revoked_before:{user_id}")
    assert revoked_ts is not None
    ttl = await fake_redis.ttl(f"auth:revoked_before:{user_id}")
    assert ttl == settings.JWT_REFRESH_EXPIRE_DAYS * 86400


@pytest.mark.asyncio
async def test_reset_password_success_tier2_otp(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    old_hash = hash_password("OldPassword123!")
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="Reset User",
        hashed_password=old_hash,
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    raw_otp = "849201"
    otp_hash = hashlib.sha256(raw_otp.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{otp_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)

    payload = ResetPasswordRequest(token=raw_otp, new_password="NewSecurePassword123!")
    response = await service.reset_password(payload)

    assert isinstance(response, ResetPasswordResponse)
    assert response.message == "Password has been reset successfully."
    assert verify_password("NewSecurePassword123!", user.hashed_password) is True


@pytest.mark.asyncio
async def test_reset_password_single_use_token_cannot_be_reused(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="Reset User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    raw_token = "single_use_token_123"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)

    await service.reset_password(
        ResetPasswordRequest(token=raw_token, new_password="NewPassword123!")
    )

    with pytest.raises(InvalidPasswordResetTokenError):
        await service.reset_password(
            ResetPasswordRequest(token=raw_token, new_password="AnotherPassword123!")
        )


@pytest.mark.asyncio
async def test_reset_password_invalid_token_raises(
    auth_service: AuthService,
) -> None:
    payload = ResetPasswordRequest(token="invalid_token", new_password="NewPassword123!")
    with pytest.raises(InvalidPasswordResetTokenError) as exc_info:
        await auth_service.reset_password(payload)

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "AUTH_INVALID_RESET_TOKEN"


@pytest.mark.asyncio
async def test_reset_password_inactive_user_raises(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="Inactive User",
        hashed_password="hash",
        role="customer",
        is_active=False,
    )
    mock_db.get.return_value = user

    raw_token = "token_for_inactive"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)
    payload = ResetPasswordRequest(token=raw_token, new_password="NewPassword123!")

    with pytest.raises(InvalidPasswordResetTokenError):
        await service.reset_password(payload)


@pytest.mark.asyncio
async def test_reset_password_user_not_found_raises(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    mock_db.get.return_value = None

    raw_token = "token_for_missing_user"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)
    payload = ResetPasswordRequest(token=raw_token, new_password="NewPassword123!")

    with pytest.raises(InvalidPasswordResetTokenError):
        await service.reset_password(payload)


@pytest.mark.asyncio
async def test_reset_password_ip_brute_force_lockout(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    ip = "192.168.1.50"
    payload = ResetPasswordRequest(token="bad_token", new_password="NewPassword123!")

    for _ in range(5):
        with pytest.raises(InvalidPasswordResetTokenError):
            await auth_service.reset_password(payload, ip=ip)

    with pytest.raises(PasswordResetRateLimitError) as exc_info:
        await auth_service.reset_password(payload, ip=ip)

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_PASSWORD_RESET_RATE_LIMITED"


@pytest.mark.asyncio
async def test_reset_password_token_brute_force_lockout(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    token = "targeted_token"
    payload = ResetPasswordRequest(token=token, new_password="NewPassword123!")

    for i in range(5):
        with pytest.raises(InvalidPasswordResetTokenError):
            await auth_service.reset_password(payload, ip=f"10.0.0.{i}")

    with pytest.raises(PasswordResetRateLimitError) as exc_info:
        await auth_service.reset_password(payload, ip="10.0.0.99")

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "AUTH_PASSWORD_RESET_RATE_LIMITED"


@pytest.mark.asyncio
async def test_refresh_token_revocation_after_password_reset(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    from datetime import UTC, datetime

    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="User",
        hashed_password="hash",
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)

    old_refresh_token = create_refresh_token(user_id, "customer", settings)

    reset_time = datetime.now(UTC).timestamp() + 1.0
    await fake_redis.set(f"auth:revoked_before:{user_id}", str(reset_time))

    with pytest.raises(InvalidTokenError) as exc_info:
        await service.refresh(old_refresh_token)

    assert exc_info.value.detail == "Token has been revoked due to password reset."

    await fake_redis.set(f"auth:revoked_before:{user_id}", str(reset_time - 100))
    new_refresh_token = create_refresh_token(user_id, "customer", settings)
    tokens = await service.refresh(new_refresh_token)
    assert tokens.access_token is not None


@pytest.mark.asyncio
async def test_reset_password_allows_login_and_refresh_within_same_second(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
        is_phone_verified=True,
    )
    mock_db.get.return_value = user
    mock_db.execute.return_value = _db_returning(user)

    raw_token = "same_second_reset_token"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)
    await service.reset_password(
        ResetPasswordRequest(token=raw_token, new_password="NewPassword123!")
    )

    tokens = await service.login(LoginRequest(phone="08153551975", password="NewPassword123!"))
    refreshed = await service.refresh(tokens.refresh_token)
    assert refreshed.access_token is not None


def test_forgot_password_request_validation() -> None:
    from pydantic import ValidationError

    # Valid with phone
    req_phone = ForgotPasswordRequest(phone="08153551975")
    assert req_phone.phone == "2348153551975"

    # Valid with email
    req_email = ForgotPasswordRequest(email="user@test.com")
    assert req_email.email == "user@test.com"

    # Missing both phone and email raises validation error
    with pytest.raises(ValidationError):
        ForgotPasswordRequest()


def test_reset_password_request_validation() -> None:
    from pydantic import ValidationError

    # Valid
    req = ResetPasswordRequest(token="token123", new_password="ValidPassword123!")
    assert req.token == "token123"

    # Weak password - no digit
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="token123", new_password="NoDigitsHere!")

    # Weak password - no uppercase
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="token123", new_password="nouppercase123!")

    # Weak password - too short (<8)
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="token123", new_password="Short1!")

    # Blank token after strip raises
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="   ", new_password="ValidPassword123!")

    # Token with whitespace is stripped
    req_strip = ResetPasswordRequest(token="  token123  ", new_password="ValidPassword123!")
    assert req_strip.token == "token123"

    # Optional phone field works
    req_with_phone = ResetPasswordRequest(
        token="token123",
        new_password="ValidPassword123!",
        phone="08153551975",
    )
    assert req_with_phone.phone == "2348153551975"


@pytest.mark.asyncio
async def test_forgot_password_rollback_acquired_identifiers_when_second_fails(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    # Pre-set rate limit for email so it fails
    email = "rate_limited@example.com"
    phone = "2348153551975"
    await fake_redis.set(f"pwd_reset:rate:{email}", "3")

    payload = ForgotPasswordRequest(phone=phone, email=email)
    with pytest.raises(PasswordResetRateLimitError):
        await auth_service.forgot_password(payload)

    # Phone cooldown must be rolled back and rate counter decremented
    assert await fake_redis.get(f"pwd_reset:cooldown:{phone}") is None
    phone_rate = await fake_redis.get(f"pwd_reset:rate:{phone}")
    assert phone_rate is None or int(phone_rate) == 0


@pytest.mark.asyncio
async def test_reset_password_phone_scoped_token_success(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    raw_token = "otp_token_phone_scoped"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:2348153551975:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)
    payload = ResetPasswordRequest(
        token=raw_token,
        phone="08153551975",
        new_password="NewSecurePassword123!",
    )
    res = await service.reset_password(payload)
    assert res.message == "Password has been reset successfully."
    assert await fake_redis.get(f"pwd_reset:token:2348153551975:{token_hash}") is None


@pytest.mark.asyncio
async def test_reset_password_mismatched_phone_fails(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    raw_token = "any_token"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)
    payload = ResetPasswordRequest(
        token=raw_token,
        phone="08011112222",  # Different phone from user.phone
        new_password="NewSecurePassword123!",
    )
    with pytest.raises(InvalidPasswordResetTokenError):
        await service.reset_password(payload)


@pytest.mark.asyncio
async def test_reset_password_clears_ip_failure_counter_on_success(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        phone="2348153551975",
        full_name="User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
    )
    mock_db.get.return_value = user

    ip = "192.168.1.99"
    service = AuthService(db=mock_db, redis_client=fake_redis, settings=settings)

    # Trigger 2 failures to populate pwd_reset:fails:ip:{ip}
    for _ in range(2):
        with pytest.raises(InvalidPasswordResetTokenError):
            await service.reset_password(
                ResetPasswordRequest(token="invalid", new_password="NewPassword123!"),
                ip=ip,
            )
    assert await fake_redis.get(f"pwd_reset:fails:ip:{ip}") == "2"

    # Now valid reset succeeds
    raw_token = "valid_token"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    await fake_redis.set(f"pwd_reset:token:{token_hash}", str(user_id))

    await service.reset_password(
        ResetPasswordRequest(token=raw_token, new_password="NewPassword123!"),
        ip=ip,
    )
    # Failure counter must be cleared
    assert await fake_redis.get(f"pwd_reset:fails:ip:{ip}") is None


def test_dev_static_otp_settings_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEV_STATIC_OTP allows 6 digits in dev/staging, normalizes blanks, and fails in prod."""
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEV_STATIC_OTP", "123456")
    assert Settings().DEV_STATIC_OTP == "123456"

    monkeypatch.setenv("DEV_STATIC_OTP", "   ")
    assert Settings().DEV_STATIC_OTP is None

    monkeypatch.setenv("DEV_STATIC_OTP", "12345")
    with pytest.raises(ValidationError):
        Settings()

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_STATIC_OTP", "123456")
    with pytest.raises(ValidationError, match="production"):
        Settings()


def test_generate_otp_honors_dev_static_otp_only_outside_production(
    settings: Settings,
) -> None:
    """Defense-in-depth: generate_otp refuses DEV_STATIC_OTP if environment is production."""
    staging_settings = settings.model_copy(
        update={"ENVIRONMENT": "staging", "DEV_STATIC_OTP": "123456"}
    )
    assert generate_otp(staging_settings) == "123456"

    forced_prod_settings = settings.model_copy(
        update={"ENVIRONMENT": "production", "DEV_STATIC_OTP": "123456"}
    )
    generated = {generate_otp(forced_prod_settings) for _ in range(5)}
    assert all(code.isdigit() and 100000 <= int(code) <= 999999 for code in generated)
    assert generated != {"123456"}


@pytest.mark.asyncio
async def test_send_otp_and_forgot_password_use_dev_static_otp(
    mock_db: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
    settings: Settings,
) -> None:
    """AuthService.send_otp and forgot_password Tier 2 SMS both honor DEV_STATIC_OTP."""
    dev_settings = settings.model_copy(
        update={"ENVIRONMENT": "staging", "DEV_STATIC_OTP": "123456"}
    )
    service = AuthService(db=mock_db, redis_client=fake_redis, settings=dev_settings)

    await _send_otp_like_router(service, "08153551975")
    assert await fake_redis.get(f"otp:{CANONICAL_PHONE}") == "123456"

    user = User(
        id=uuid.uuid4(),
        phone=CANONICAL_PHONE,
        full_name="Dev OTP User",
        hashed_password=hash_password("OldPassword123!"),
        role="customer",
        is_active=True,
        email=None,
        email_verified_at=None,
    )
    lookup_result = MagicMock()
    lookup_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = lookup_result

    await service.forgot_password(ForgotPasswordRequest(phone="08153551975"))
    expected_hash = hashlib.sha256(b"123456").hexdigest()
    assert await fake_redis.get(f"pwd_reset:token:{expected_hash}") == str(user.id)
