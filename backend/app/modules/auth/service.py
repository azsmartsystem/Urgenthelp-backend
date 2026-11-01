"""Auth service — business logic for registration, login, OTP, and token management.

No HTTP awareness here. Receives typed inputs, returns typed outputs,
raises typed exceptions.
"""

import secrets
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidOTPError,
    InvalidTokenError,
    OTPCooldownError,
    OTPExpiredError,
    OTPRateLimitError,
    PhoneAlreadyRegisteredError,
    PhoneNotVerifiedError,
    UserNotFoundError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.validators import CanonicalPhone
from app.modules.auth.schemas import (
    LoginRequest,
    OTPVerifyRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from app.modules.notifications.service import NotificationService
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
        notification_service: NotificationService | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._settings = settings or get_settings()
        self._notification_service = notification_service or NotificationService(
            settings=self._settings
        )

    async def register(self, payload: RegisterRequest) -> RegisterResponse:
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
            is_phone_verified=False,
            trust_score=50.0,
        )
        self._db.add(user)

        try:
            await self.send_otp(CanonicalPhone(payload.phone))
            await self._db.commit()
            await self._db.refresh(user)
        except Exception:
            await self._redis.delete(f"otp:{payload.phone}")
            await self._redis.delete(f"otp:cooldown:{payload.phone}")
            rate_val = await self._redis.get(f"otp:rate:{payload.phone}")
            if rate_val is not None and int(rate_val) > 0:
                await self._redis.decr(f"otp:rate:{payload.phone}")
            await self._db.rollback()
            raise

        logger.info("user_registered", user_id=str(user.id), role=user.role)

        return RegisterResponse(
            id=user.id,
            phone=user.phone,
            full_name=user.full_name,
            role=payload.role,
            email=payload.email,
            is_phone_verified=user.is_phone_verified,
        )

    async def login(self, payload: LoginRequest) -> TokenResponse:
        """Authenticate user by phone and password."""
        query = select(User).where(User.phone == payload.phone)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()
        if user is None or not verify_password(payload.password, user.hashed_password):
            raise InvalidCredentialsError()

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user.id)})

        if not user.is_phone_verified:
            raise PhoneNotVerifiedError(
                context={"phone": payload.phone[-4:], "user_id": str(user.id)}
            )

        logger.info("user_logged_in", user_id=str(user.id), role=user.role)
        access_token = create_access_token(user.id, user.role, self._settings)
        refresh_token = create_refresh_token(user.id, user.role, self._settings)
        return TokenResponse(access_token=access_token, refresh_token=refresh_token)

    async def send_otp(self, phone: CanonicalPhone) -> None:
        """Generate and store an OTP in Redis, dispatching it to the user.

        The CanonicalPhone annotation is the guarantee that `phone` is already
        in canonical form: normalization happens once at the schema boundary via
        the NigerianPhone annotated type, and mypy rejects any caller that tries
        to pass a raw "0815… " string. Without it, such a caller would write an
        `otp:0815…` key that verify_otp — which looks up the canonical form —
        could never find.
        """
        rate_key = f"otp:rate:{phone}"
        cooldown_key = f"otp:cooldown:{phone}"

        current_count = await self._redis.get(rate_key)
        if (
            current_count is not None
            and int(current_count) >= self._settings.OTP_MAX_REQUESTS_PER_WINDOW
        ):
            rate_ttl = await self._redis.ttl(rate_key)
            retry_after = max(int(rate_ttl), 1)
            raise OTPRateLimitError(
                detail=(
                    f"Too many OTP requests. Maximum {self._settings.OTP_MAX_REQUESTS_PER_WINDOW} "
                    f"requests per {self._settings.OTP_RATE_LIMIT_WINDOW_MINUTES} minutes."
                ),
                context={"phone": phone[-4:], "retry_after": retry_after},
            )

        # Atomically acquire cooldown lock upfront (SET ... EX ... NX)
        cooldown_acquired = await self._redis.set(
            cooldown_key,
            "1",
            ex=self._settings.OTP_COOLDOWN_SECONDS,
            nx=True,
        )
        if not cooldown_acquired:
            cooldown_ttl = await self._redis.ttl(cooldown_key)
            retry_after = max(int(cooldown_ttl), 1)
            raise OTPCooldownError(
                detail=f"Please wait {retry_after} seconds before requesting another OTP.",
                context={"phone": phone[-4:], "retry_after": retry_after},
            )

        current_requests = await self._redis.incr(rate_key)
        rate_window_seconds = self._settings.OTP_RATE_LIMIT_WINDOW_MINUTES * 60
        if current_requests == 1 or await self._redis.ttl(rate_key) == -1:
            await self._redis.expire(rate_key, rate_window_seconds)

        if current_requests > self._settings.OTP_MAX_REQUESTS_PER_WINDOW:
            await self._redis.delete(cooldown_key)
            rate_ttl = await self._redis.ttl(rate_key)
            retry_after = max(int(rate_ttl), 1)
            raise OTPRateLimitError(
                detail=(
                    f"Too many OTP requests. Maximum {self._settings.OTP_MAX_REQUESTS_PER_WINDOW} "
                    f"requests per {self._settings.OTP_RATE_LIMIT_WINDOW_MINUTES} minutes."
                ),
                context={"phone": phone[-4:], "retry_after": retry_after},
            )

        otp = f"{secrets.randbelow(900000) + 100000}"
        redis_key = f"otp:{phone}"
        ttl_seconds = self._settings.OTP_EXPIRE_MINUTES * 60

        await self._redis.set(redis_key, otp, ex=ttl_seconds)

        try:
            await self._notification_service.send_otp_sms(
                to=phone,
                otp=otp,
                expire_minutes=self._settings.OTP_EXPIRE_MINUTES,
            )
        except Exception:
            await self._redis.delete(redis_key)
            await self._redis.delete(cooldown_key)
            await self._redis.decr(rate_key)
            raise

        if self._settings.is_production:
            logger.info("otp_dispatched", phone=f"...{phone[-4:]}")
        else:
            logger.info("otp_generated_dev", phone=phone, otp=otp, ttl=ttl_seconds)

    async def verify_otp(self, payload: OTPVerifyRequest) -> TokenResponse:
        """Verify an OTP and return JWT tokens upon success."""
        # Must match the key AuthService.send_otp wrote, which is why both sides
        # narrow through CanonicalPhone: a raw "0815…" here would look up a key
        # that send_otp never created.
        phone = CanonicalPhone(payload.phone)
        redis_key = f"otp:{phone}"
        stored_otp = await self._redis.get(redis_key)
        if stored_otp is None:
            raise OTPExpiredError(context={"phone": phone[-4:]})

        if not secrets.compare_digest(str(stored_otp), payload.otp):
            raise InvalidOTPError(context={"phone": phone[-4:]})

        query = select(User).where(User.phone == phone)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(
                detail="No account found with this phone number. Please register first.",
                context={"phone": phone[-4:]},
            )

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user.id)})

        if not user.is_phone_verified:
            user.is_phone_verified = True
            user.phone_verified_at = datetime.now(UTC)
            await self._db.commit()
            await self._db.refresh(user)

        # Consume the code only once verification is persisted and account is known good.
        # If DB commit fails, the OTP remains in Redis for the user to retry.
        await self._redis.delete(redis_key)

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
