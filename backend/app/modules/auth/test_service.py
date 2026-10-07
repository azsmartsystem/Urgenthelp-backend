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
from app.modules.auth.schemas import (
    LoginRequest,
    OTPVerifyRequest,
    RegisterRequest,
)
from app.modules.auth.service import AuthService
from app.modules.users.model import User
from fastapi.security import HTTPAuthorizationCredentials


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
    phone = "+2348012345678"
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
    phone = "+2348012345678"
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
    phone = "+2348012345678"
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
    payload = OTPVerifyRequest(phone="+2348012345678", otp="123456")
    with pytest.raises(OTPExpiredError):
        await auth_service.verify_otp(payload)


@pytest.mark.asyncio
async def test_verify_otp_invalid_raises(
    auth_service: AuthService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    phone = "+2348012345678"
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
    phone = "+2348012345678"
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
    phone = "+2348012345678"
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
