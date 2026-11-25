"""Auth service — business logic for registration, login, OTP, and token management.

No HTTP awareness here. Receives typed inputs, returns typed outputs,
raises typed exceptions.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from app.core.config import Settings, get_settings
from app.core.email_verification import (
    clear_email_verification_otp,
    dispatch_email_verification_otp,
    mask_email,
    rollback_email_verification_otp,
    verify_email_verification_otp,
)
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    EmailOTPExpiredError,
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
    PhoneAlreadyVerifiedError,
    PhoneCheckRateLimitError,
    PhoneNotVerifiedError,
    UserNotFoundError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_otp,
    hash_password,
    verify_password,
)
from app.core.validators import CanonicalPhone
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    OTPVerifyRequest,
    PhoneCheckResponse,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    TokenResponse,
    VerifyEmailOTPRequest,
)
from app.modules.notifications.service import NotificationService
from app.modules.notifications.verification import (
    PhoneVerifyChannel,
    VerificationProvider,
    get_twilio_verify_provider,
)
from app.modules.users.model import User
from sqlalchemy import func, select
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
        verification_provider: VerificationProvider | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._settings = settings or get_settings()
        self._notification_service = notification_service or NotificationService(
            settings=self._settings
        )
        if verification_provider is not None:
            self._verification_provider: VerificationProvider | None = verification_provider
        elif self._settings.OTP_PROVIDER == "twilio_verify":
            self._verification_provider = get_twilio_verify_provider(self._settings)
        else:
            self._verification_provider = None

    async def check_phone(self, phone: CanonicalPhone, ip: str = "127.0.0.1") -> PhoneCheckResponse:
        """Validate a normalized Nigerian phone and check whether it is already registered."""
        now = datetime.now(UTC).timestamp()
        window_start = now - 60
        ip_rate_key = f"phone_check:rate:ip:{ip}"
        member = f"{now}:{uuid.uuid4()}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(ip_rate_key, 0, window_start)
            pipe.zadd(ip_rate_key, {member: now})
            pipe.zcard(ip_rate_key)
            pipe.expire(ip_rate_key, 60)
            _, _, ip_request_count, _ = await pipe.execute()

        if ip_request_count > 10:
            await self._redis.zrem(ip_rate_key, member)
            raise PhoneCheckRateLimitError(
                detail="Too many phone check requests from this IP. Please try again later.",
                context={"ip": ip, "retry_after": 60},
            )

        existing = await self._db.execute(select(User.id).where(User.phone == phone))
        is_registered = existing.scalar_one_or_none() is not None
        return PhoneCheckResponse(phone=phone, is_registered=is_registered)

    async def register(self, payload: RegisterRequest) -> RegisterResponse:
        """Register a new customer or helper account."""
        query = select(User).where(User.phone == payload.phone)
        result = await self._db.execute(query)
        if result.scalar_one_or_none() is not None:
            raise PhoneAlreadyRegisteredError(context={"phone": payload.phone[-4:]})

        email_str = str(payload.email).strip().lower()
        email_query = select(User).where(func.lower(User.email) == email_str)
        email_result = await self._db.execute(email_query)
        if email_result.scalar_one_or_none() is not None:
            raise EmailAlreadyRegisteredError(context={"email": email_str})

        hashed_pwd = hash_password(payload.password)
        user = User(
            id=uuid.uuid4(),
            phone=payload.phone,
            email=email_str,
            full_name=payload.full_name,
            hashed_password=hashed_pwd,
            role=payload.role,
            is_active=True,
            is_id_verified=False,
            is_phone_verified=False,
            trust_score=50.0,
        )
        self._db.add(user)

        phone_dispatched = False
        email_dispatched = False
        try:
            await self.send_otp(
                CanonicalPhone(payload.phone),
                check_user_exists=False,
                channel=payload.otp_channel,
            )
            phone_dispatched = True
            await dispatch_email_verification_otp(
                redis_client=self._redis,
                settings=self._settings,
                notification_service=self._notification_service,
                user_id=user.id,
                email=email_str,
            )
            email_dispatched = True
            await self._db.commit()
            await self._db.refresh(user)
        except Exception:
            if phone_dispatched:
                await self._redis.delete(f"otp:{payload.phone}")
                await self._redis.delete(f"otp:cooldown:{payload.phone}")
                rate_val = await self._redis.get(f"otp:rate:{payload.phone}")
                if rate_val is not None and int(rate_val) > 0:
                    await self._redis.decr(f"otp:rate:{payload.phone}")
            if email_dispatched:
                await rollback_email_verification_otp(self._redis, user.id)
            await self._db.rollback()
            raise

        logger.info("user_registered", user_id=str(user.id), role=user.role)

        return RegisterResponse(
            id=user.id,
            phone=user.phone,
            full_name=user.full_name,
            role=payload.role,
            email=email_str,
            is_phone_verified=user.is_phone_verified,
            is_email_verified=user.email_verified_at is not None,
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

    async def _get_active_user_by_phone(self, phone: CanonicalPhone) -> User:
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
        return user

    async def send_otp(
        self,
        phone: CanonicalPhone,
        *,
        check_user_exists: bool = True,
        channel: PhoneVerifyChannel | None = None,
    ) -> None:
        """Generate and dispatch a phone OTP via Twilio Verify or Redis + SMS.

        The CanonicalPhone annotation is the guarantee that `phone` is already
        in canonical form: normalization happens once at the schema boundary via
        the NigerianPhone annotated type, and mypy rejects any caller that tries
        to pass a raw "0815… " string. Without it, such a caller would write an
        `otp:0815…` key that verify_otp — which looks up the canonical form —
        could never find.
        """
        if check_user_exists:
            user = await self._get_active_user_by_phone(phone)
            if user.is_phone_verified:
                raise PhoneAlreadyVerifiedError(
                    detail="Phone number is already verified. Please log in.",
                    context={"phone": phone[-4:]},
                )

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

        resolved_channel: PhoneVerifyChannel = (
            channel or self._settings.TWILIO_VERIFY_DEFAULT_CHANNEL
        )
        if self._verification_provider is not None:
            try:
                await self._verification_provider.start_verification(
                    to=phone,
                    channel=resolved_channel,
                )
            except Exception:
                await self._redis.delete(cooldown_key)
                await self._redis.decr(rate_key)
                raise

            logger.info(
                "otp_dispatched",
                phone=f"...{phone[-4:]}",
                channel=resolved_channel,
                provider="twilio_verify",
            )
            return

        otp = generate_otp(self._settings)
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
        if self._verification_provider is not None:
            # Verify account existence & status before burning a Twilio check attempt.
            user = await self._get_active_user_by_phone(phone)
            approved = await self._verification_provider.check_verification(
                to=phone,
                code=payload.otp,
                channel="sms",
            )
            if not approved:
                raise InvalidOTPError(context={"phone": phone[-4:]})

            if not user.is_phone_verified:
                user.is_phone_verified = True
                user.phone_verified_at = datetime.now(UTC)
                await self._db.commit()
                await self._db.refresh(user)

            logger.info(
                "otp_verified",
                user_id=str(user.id),
                role=user.role,
                provider="twilio_verify",
            )
            access_token = create_access_token(user.id, user.role, self._settings)
            refresh_token = create_refresh_token(user.id, user.role, self._settings)
            return TokenResponse(access_token=access_token, refresh_token=refresh_token)

        redis_key = f"otp:{phone}"
        stored_otp = await self._redis.get(redis_key)
        if stored_otp is None:
            raise OTPExpiredError(context={"phone": phone[-4:]})

        if not secrets.compare_digest(str(stored_otp), payload.otp):
            raise InvalidOTPError(context={"phone": phone[-4:]})

        user = await self._get_active_user_by_phone(phone)

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

    async def send_email_otp(self, email: str) -> None:
        """Send an email verification OTP to an unverified active account."""
        email_str = email.strip().lower()
        query = select(User).where(func.lower(User.email) == email_str)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()
        if user is None or not user.is_active or user.email_verified_at is not None:
            return

        await dispatch_email_verification_otp(
            redis_client=self._redis,
            settings=self._settings,
            notification_service=self._notification_service,
            user_id=user.id,
            email=email_str,
        )

    async def verify_email_otp(self, payload: VerifyEmailOTPRequest) -> TokenResponse:
        """Verify an email OTP and return JWT tokens once both channels are verified."""
        email_str = str(payload.email).strip().lower()
        query = select(User).where(func.lower(User.email) == email_str)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()
        if user is None:
            raise EmailOTPExpiredError()

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user.id)})

        await verify_email_verification_otp(
            redis_client=self._redis,
            settings=self._settings,
            user_id=user.id,
            submitted_otp=payload.otp,
            expected_email=email_str,
        )

        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(UTC)
            await self._db.commit()
            await self._db.refresh(user)

        await clear_email_verification_otp(self._redis, user.id)

        if self._settings.is_production:
            logger.info("email_verified", user_id=str(user.id), email=mask_email(email_str))
        else:
            logger.info("email_verified", user_id=str(user.id), email=email_str)

        if not user.is_phone_verified:
            raise PhoneNotVerifiedError(context={"phone": user.phone[-4:], "user_id": str(user.id)})

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

        revoked_before = await self._redis.get(f"auth:revoked_before:{payload.sub}")
        if revoked_before is not None:
            if payload.iat.timestamp() < float(revoked_before):
                raise InvalidTokenError(detail="Token has been revoked due to password reset.")

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

    async def logout(self, refresh_token: str) -> None:
        """Revoke a refresh token so it can no longer mint new tokens."""
        payload = decode_token(
            refresh_token,
            self._settings.JWT_REFRESH_SECRET,
            allow_expired=True,
        )
        if payload.type != "refresh":
            raise InvalidTokenError(detail="Expected a refresh token.")

        remaining_ttl = int(payload.exp.timestamp() - datetime.now(UTC).timestamp())
        if remaining_ttl <= 0:
            logger.info("user_logged_out_expired_token", user_id=payload.sub)
            return

        blacklist_key = f"blacklist:{refresh_token}"
        await self._redis.set(blacklist_key, "1", ex=remaining_ttl)
        logger.info("user_logged_out", user_id=payload.sub)

    async def _rollback_pwd_reset_limits(self, identifier: str) -> None:
        await self._redis.delete(f"pwd_reset:cooldown:{identifier}")
        rate_key = f"pwd_reset:rate:{identifier}"
        rate_val = await self._redis.get(rate_key)
        if rate_val is not None and int(rate_val) > 0:
            await self._redis.decr(rate_key)

    async def forgot_password(
        self, payload: ForgotPasswordRequest, ip: str = "127.0.0.1"
    ) -> ForgotPasswordResponse:
        """Initiate verified-email-only password recovery with anti-enumeration and rate limits."""
        # 1. Atomic IP rate limit: sliding window (max 10 requests per minute)
        now = datetime.now(UTC).timestamp()
        window_start = now - 60
        ip_rate_key = f"pwd_reset:rate:ip:{ip}"
        member = f"{now}:{uuid.uuid4()}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(ip_rate_key, 0, window_start)
            pipe.zadd(ip_rate_key, {member: now})
            pipe.zcard(ip_rate_key)
            pipe.expire(ip_rate_key, 60)
            _, _, ip_request_count, _ = await pipe.execute()

        if ip_request_count > 10:
            await self._redis.zrem(ip_rate_key, member)
            raise PasswordResetRateLimitError(
                detail="Too many password reset requests from this IP. Please try again later.",
                context={"ip": ip, "retry_after": 60},
            )

        # 2. Per-email rate limiting & cooldown
        email_str = str(payload.email).strip().lower()
        rate_key = f"pwd_reset:rate:{email_str}"
        cooldown_key = f"pwd_reset:cooldown:{email_str}"

        current_count = await self._redis.get(rate_key)
        if current_count is not None and int(current_count) >= 3:
            rate_ttl = await self._redis.ttl(rate_key)
            retry_after = max(int(rate_ttl), 1)
            raise PasswordResetRateLimitError(
                detail="Too many password reset requests. Maximum 3 requests per 10 minutes.",
                context={"identifier": email_str, "retry_after": retry_after},
            )

        cooldown_acquired = await self._redis.set(cooldown_key, "1", ex=60, nx=True)
        if not cooldown_acquired:
            cooldown_ttl = await self._redis.ttl(cooldown_key)
            retry_after = max(int(cooldown_ttl), 1)
            raise PasswordResetCooldownError(
                detail=(
                    f"Please wait {retry_after} seconds before requesting another password reset."
                ),
                context={"identifier": email_str, "retry_after": retry_after},
            )

        try:
            current_requests = await self._redis.incr(rate_key)
            if current_requests == 1 or await self._redis.ttl(rate_key) == -1:
                await self._redis.expire(rate_key, 600)

            if current_requests > 3:
                await self._redis.delete(cooldown_key)
                rate_ttl = await self._redis.ttl(rate_key)
                retry_after = max(int(rate_ttl), 1)
                raise PasswordResetRateLimitError(
                    detail="Too many password reset requests. Maximum 3 requests per 10 minutes.",
                    context={"identifier": email_str, "retry_after": retry_after},
                )
        except Exception:
            await self._redis.delete(cooldown_key)
            raise

        # 3. Lookup user by email
        query = select(User).where(func.lower(User.email) == email_str)
        result = await self._db.execute(query)
        user = result.scalar_one_or_none()

        # Anti-enumeration: identical 200 response if missing, inactive, or email unverified
        if user is None or not user.is_active:
            hashlib.sha256(b"dummy_padding_constant_time").hexdigest()
            return ForgotPasswordResponse()

        if user.email is None or user.email_verified_at is None:
            logger.warning(
                "pwd_reset_unverified_email_blocked",
                user_id=str(user.id),
                email=mask_email(email_str),
            )
            hashlib.sha256(b"dummy_padding_constant_time").hexdigest()
            return ForgotPasswordResponse()

        # 4. Verified email token dispatch (revokes any prior active token on success)
        ttl_seconds = self._settings.PASSWORD_RESET_EXPIRE_MINUTES * 60
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token_key = f"pwd_reset:token:{token_hash}"
        active_key = f"pwd_reset:active:{user.id}"

        try:
            await self._redis.set(token_key, str(user.id), ex=ttl_seconds)
            try:
                await self._notification_service.send_password_reset_email(
                    to=user.email,
                    token=raw_token,
                    expire_minutes=self._settings.PASSWORD_RESET_EXPIRE_MINUTES,
                )
            except Exception:
                await self._redis.delete(token_key)
                raise
        except Exception:
            await self._rollback_pwd_reset_limits(email_str)
            raise

        prev_hash = await self._redis.get(active_key)
        if prev_hash is not None:
            prev_hash_str = (
                prev_hash.decode("utf-8") if isinstance(prev_hash, bytes) else str(prev_hash)
            )
            if prev_hash_str != token_hash:
                await self._redis.delete(f"pwd_reset:token:{prev_hash_str}")
        await self._redis.set(active_key, token_hash, ex=ttl_seconds)

        if not self._settings.is_production:
            logger.info("pwd_reset_token_dev", user_id=str(user.id), token=raw_token)

        return ForgotPasswordResponse()

    async def reset_password(
        self, payload: ResetPasswordRequest, ip: str = "127.0.0.1"
    ) -> ResetPasswordResponse:
        """Verify reset token, reset password, delete token, and revoke existing sessions."""
        ip_lockout_key = f"pwd_reset:lockout:ip:{ip}"
        if await self._redis.get(ip_lockout_key):
            ttl = await self._redis.ttl(ip_lockout_key)
            raise PasswordResetRateLimitError(
                detail="Too many failed reset attempts from this IP. Please try again later.",
                context={"ip": ip, "retry_after": max(int(ttl), 1)},
            )

        token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
        token_lockout_key = f"pwd_reset:lockout:token:{token_hash}"
        if await self._redis.get(token_lockout_key):
            ttl = await self._redis.ttl(token_lockout_key)
            raise PasswordResetRateLimitError(
                detail="Too many failed attempts with this reset token. Please request a new one.",
                context={"retry_after": max(int(ttl), 1)},
            )

        token_key = f"pwd_reset:token:{token_hash}"
        user_id_str = await self._redis.getdel(token_key)

        if user_id_str is None:
            # Register failed attempt for IP and token
            ip_fail_key = f"pwd_reset:fails:ip:{ip}"
            token_fail_key = f"pwd_reset:fails:token:{token_hash}"

            ip_fails = await self._redis.incr(ip_fail_key)
            if ip_fails == 1 or await self._redis.ttl(ip_fail_key) == -1:
                await self._redis.expire(ip_fail_key, 900)
            if ip_fails >= 5:
                await self._redis.set(ip_lockout_key, "1", ex=900)

            token_fails = await self._redis.incr(token_fail_key)
            if token_fails == 1 or await self._redis.ttl(token_fail_key) == -1:
                await self._redis.expire(token_fail_key, 900)
            if token_fails >= 5:
                await self._redis.set(token_lockout_key, "1", ex=900)

            raise InvalidPasswordResetTokenError()

        user_uuid = uuid.UUID(str(user_id_str))
        user = await self._db.get(User, user_uuid)
        if user is None or not user.is_active:
            raise InvalidPasswordResetTokenError()

        user.hashed_password = hash_password(payload.new_password)
        await self._db.commit()
        await self._db.refresh(user)

        # Invalidate all existing refresh tokens (integer seconds to match JWT iat resolution)
        now_ts = int(datetime.now(UTC).timestamp())
        revocation_ttl = self._settings.JWT_REFRESH_EXPIRE_DAYS * 86400
        await self._redis.set(f"auth:revoked_before:{user.id}", str(now_ts), ex=revocation_ttl)

        # Clear active reset token pointer and failure counters on success
        await self._redis.delete(f"pwd_reset:active:{user.id}")
        await self._redis.delete(f"pwd_reset:fails:token:{token_hash}")
        await self._redis.delete(f"pwd_reset:fails:ip:{ip}")

        logger.info("password_reset_success", user_id=str(user.id))
        return ResetPasswordResponse(message="Password has been reset successfully.")
