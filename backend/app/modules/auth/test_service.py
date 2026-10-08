"""Unit tests for AuthService and security utilities."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    ForbiddenError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidOTPError,
    InvalidTokenError,
    OTPExpiredError,
    PhoneAlreadyRegisteredError,
    UserNotFoundError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    require_role,
    verify_password,
)
from app.core.validators import CanonicalPhone
from app.modules.auth.schemas import (
    LoginRequest,
    OTPVerifyRequest,
    RegisterRequest,
    SendOTPRequest,
)
from app.modules.auth.service import AuthService
from app.modules.users.model import User
from fastapi.security import HTTPAuthorizationCredentials
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
    mock_db: AsyncMock,
    settings: Settings,
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

    tokens = await auth_service.register(payload)

    assert tokens.access_token is not None
    assert tokens.refresh_token is not None
    assert mock_db.add.called
    assert mock_db.commit.called

    access_payload = decode_token(tokens.access_token, settings.JWT_ACCESS_SECRET)
    assert access_payload.role == "customer"
    assert access_payload.type == "access"


@pytest.mark.asyncio
async def test_register_helper_without_email_success(
    auth_service: AuthService,
    mock_db: AsyncMock,
    settings: Settings,
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

    tokens = await auth_service.register(payload)

    assert tokens.access_token is not None
    assert tokens.refresh_token is not None
    access_payload = decode_token(tokens.access_token, settings.JWT_ACCESS_SECRET)
    assert access_payload.role == "helper"


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
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute.return_value = mock_result

    payload = OTPVerifyRequest(phone=phone, otp=otp)
    tokens = await auth_service.verify_otp(payload)

    assert tokens.access_token is not None
    # Verify OTP was deleted from redis
    assert await fake_redis.get(f"otp:{phone}") is None


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
