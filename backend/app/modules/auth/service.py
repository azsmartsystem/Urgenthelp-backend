"""Auth service — business logic for registration, login, OTP, and token management.

No HTTP awareness here. Receives typed inputs, returns typed outputs,
raises typed exceptions.
"""

import secrets
import uuid

import redis.asyncio as aioredis
import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
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
    hash_password,
    verify_password,
)
from app.core.validators import normalize_nigerian_phone
from app.modules.auth.schemas import (
    LoginRequest,
    OTPVerifyRequest,
    RegisterRequest,
    TokenResponse,
)
from app.modules.users.model import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class AuthService:
    """Encapsulates all authentication workflows."""

    def __init__(
        self,
        db: AsyncSession,
        redis_client: aioredis.Redis,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._settings = settings or get_settings()

    async def register(self, payload: RegisterRequest) -> TokenResponse:
        """Register a new customer or helper account."""
        query = select(User).where(User.phone == payload.phone)
        result = await self._db.execute(query)
        if result.scalar_one_or_none() is not None:
            raise PhoneAlreadyRegisteredError(context={"phone": payload.phone[-4:]})

        if payload.email is not None:
            email_query = select(User).where(User.email == payload.email)
            email_result = await self._db.execute(email_query)
            if email_result.scalar_one_or_none() is not None:
                raise EmailAlreadyRegisteredError(context={"email": payload.email})

        hashed_pwd = hash_password(payload.password)
        user = User(
            id=uuid.uuid4(),
            phone=payload.phone,
            email=str(payload.email) if payload.email is not None else None,
            full_name=payload.full_name,
            hashed_password=hashed_pwd,
            role=payload.role,
            is_active=True,
            is_id_verified=False,
            trust_score=50.0,
        )
        self._db.add(user)
        await self._db.commit()
        await self._db.refresh(user)

        logger.info("user_registered", user_id=str(user.id), role=user.role)

        access_token = create_access_token(user.id, user.role, self._settings)
        refresh_token = create_refresh_token(user.id, user.role, self._settings)
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def login(self, payload: LoginRequest) -> TokenResponse:
        """Authenticate user by phone and password."""
        query = select(User).where(User.phone == payload.phone)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()
        if user is None or not verify_password(payload.password, user.hashed_password):
            raise InvalidCredentialsError()

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user.id)})

        logger.info("user_logged_in", user_id=str(user.id), role=user.role)
        access_token = create_access_token(user.id, user.role, self._settings)
        refresh_token = create_refresh_token(user.id, user.role, self._settings)
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def send_otp(self, phone: str) -> None:
        """Generate and store an OTP in Redis, dispatching it to the user."""
        phone = normalize_nigerian_phone(phone)
        otp = f"{secrets.randbelow(900000) + 100000}"
        redis_key = f"otp:{phone}"
        ttl_seconds = self._settings.OTP_EXPIRE_MINUTES * 60

        await self._redis.set(redis_key, otp, ex=ttl_seconds)

        if self._settings.is_production:
            logger.info("otp_dispatched", phone=f"...{phone[-4:]}")
        else:
            logger.info("otp_generated_dev", phone=phone, otp=otp, ttl=ttl_seconds)

    async def verify_otp(self, payload: OTPVerifyRequest) -> TokenResponse:
        """Verify an OTP and return JWT tokens upon success."""
        redis_key = f"otp:{payload.phone}"
        stored_otp = await self._redis.get(redis_key)
        if stored_otp is None:
            raise OTPExpiredError(context={"phone": payload.phone[-4:]})

        if str(stored_otp) != payload.otp:
            raise InvalidOTPError(context={"phone": payload.phone[-4:]})

        await self._redis.delete(redis_key)

        query = select(User).where(User.phone == payload.phone)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(
                detail="No account found with this phone number. Please register first.",
                context={"phone": payload.phone[-4:]},
            )

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user.id)})

        logger.info("otp_verified", user_id=str(user.id), role=user.role)
        access_token = create_access_token(user.id, user.role, self._settings)
        refresh_token = create_refresh_token(user.id, user.role, self._settings)
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Rotate a refresh token and issue a fresh token pair."""
        payload = decode_token(refresh_token, self._settings.JWT_REFRESH_SECRET)
        if payload.type != "refresh":
            raise InvalidTokenError(detail="Expected a refresh token.")

        blacklist_key = f"blacklist:{refresh_token}"
        is_blacklisted = await self._redis.get(blacklist_key)
        if is_blacklisted:
            raise InvalidTokenError(detail="Token has already been revoked or refreshed.")

        user_uuid = uuid.UUID(payload.sub)
        user = await self._db.get(User, user_uuid)
        if user is None:
            raise UserNotFoundError(context={"user_id": payload.sub})

        if not user.is_active:
            raise InactiveUserError(context={"user_id": payload.sub})

        blacklist_ttl = self._settings.JWT_REFRESH_EXPIRE_DAYS * 86400
        await self._redis.set(blacklist_key, "1", ex=blacklist_ttl)

        new_access = create_access_token(user.id, user.role, self._settings)
        new_refresh = create_refresh_token(user.id, user.role, self._settings)
        logger.info("tokens_refreshed", user_id=str(user.id))
        return TokenResponse(access_token=new_access, refresh_token=new_refresh)
