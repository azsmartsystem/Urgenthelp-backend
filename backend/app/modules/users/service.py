"""User service — business logic for user account and email verification lifecycle.

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
    EmailCooldownError,
    EmailOTPExpiredError,
    EmailRateLimitError,
    InactiveUserError,
    InvalidEmailOTPError,
    UserNotFoundError,
)
from app.modules.notifications.service import NotificationService
from app.modules.users.model import User
from app.modules.users.schemas import (
    EmailVerificationResponse,
    UpdateEmailRequest,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


def _mask_email(email: str) -> str:
    """Mask email for privacy in production structured logs."""
    parts = email.split("@", 1)
    if len(parts) != 2:
        return "***"
    name, domain = parts
    masked_name = name[0] + "***" if len(name) > 1 else "***"
    return f"{masked_name}@{domain}"


class UserService:
    """Encapsulates all user profile and verification workflows."""

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

    async def request_email_verification(
        self,
        user_id: uuid.UUID,
        payload: UpdateEmailRequest,
    ) -> EmailVerificationResponse:
        """Initiate email verification lifecycle for the specified user."""
        user = await self._db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(context={"user_id": str(user_id)})

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user_id)})

        email_str = str(payload.email).strip().lower()

        # Case-insensitive uniqueness check: ensure no other user has claimed/verified this email
        query = select(User).where(func.lower(User.email) == email_str, User.id != user_id)
        result = await self._db.execute(query)
        if result.scalar_one_or_none() is not None:
            raise EmailAlreadyRegisteredError(context={"email": email_str})

        rate_key = f"email:rate:{user_id}"
        cooldown_key = f"email:cooldown:{user_id}"

        # 1. Check rate limit window
        current_count = await self._redis.get(rate_key)
        if (
            current_count is not None
            and int(current_count) >= self._settings.EMAIL_MAX_REQUESTS_PER_WINDOW
        ):
            rate_ttl = await self._redis.ttl(rate_key)
            retry_after = max(int(rate_ttl), 1)
            max_req = self._settings.EMAIL_MAX_REQUESTS_PER_WINDOW
            win_min = self._settings.EMAIL_RATE_LIMIT_WINDOW_MINUTES
            raise EmailRateLimitError(
                detail=(
                    f"Too many email verification requests. Maximum {max_req} "
                    f"requests per {win_min} minutes."
                ),
                context={"user_id": str(user_id), "retry_after": retry_after},
            )

        # 2. Acquire cooldown lock atomically (SET ... EX ... NX)
        cooldown_acquired = await self._redis.set(
            cooldown_key,
            "1",
            ex=self._settings.EMAIL_COOLDOWN_SECONDS,
            nx=True,
        )
        if not cooldown_acquired:
            cooldown_ttl = await self._redis.ttl(cooldown_key)
            retry_after = max(int(cooldown_ttl), 1)
            raise EmailCooldownError(
                detail=(
                    f"Please wait {retry_after} seconds before requesting "
                    "another email verification code."
                ),
                context={"user_id": str(user_id), "retry_after": retry_after},
            )

        # 3. Increment sliding window attempt counter
        current_requests = await self._redis.incr(rate_key)
        rate_window_seconds = self._settings.EMAIL_RATE_LIMIT_WINDOW_MINUTES * 60
        if current_requests == 1 or await self._redis.ttl(rate_key) == -1:
            await self._redis.expire(rate_key, rate_window_seconds)

        if current_requests > self._settings.EMAIL_MAX_REQUESTS_PER_WINDOW:
            await self._redis.delete(cooldown_key)
            rate_ttl = await self._redis.ttl(rate_key)
            retry_after = max(int(rate_ttl), 1)
            max_req = self._settings.EMAIL_MAX_REQUESTS_PER_WINDOW
            win_min = self._settings.EMAIL_RATE_LIMIT_WINDOW_MINUTES
            raise EmailRateLimitError(
                detail=(
                    f"Too many email verification requests. Maximum {max_req} "
                    f"requests per {win_min} minutes."
                ),
                context={"user_id": str(user_id), "retry_after": retry_after},
            )

        # 4. Generate 6-digit OTP and store state in Redis
        otp = f"{secrets.randbelow(900000) + 100000}"
        otp_key = f"email_otp:{user_id}"
        pending_key = f"pending_email:{user_id}"
        fail_key = f"email_otp_fails:{user_id}"
        ttl_seconds = self._settings.EMAIL_VERIFICATION_EXPIRE_HOURS * 3600

        # 5. Store OTP in Redis and dispatch via NotificationService (rollback keys on failure)
        try:
            await self._redis.set(otp_key, otp, ex=ttl_seconds)
            await self._redis.set(pending_key, email_str, ex=ttl_seconds)
            await self._redis.delete(fail_key)
            await self._notification_service.send_verification_email(
                to=email_str,
                otp=otp,
                expire_hours=self._settings.EMAIL_VERIFICATION_EXPIRE_HOURS,
            )
        except Exception:
            await self._redis.delete(otp_key)
            await self._redis.delete(pending_key)
            await self._redis.delete(fail_key)
            await self._redis.delete(cooldown_key)
            rate_val = await self._redis.get(rate_key)
            if rate_val is not None and int(rate_val) > 0:
                await self._redis.decr(rate_key)
            raise

        if self._settings.is_production:
            logger.info(
                "email_verification_dispatched",
                user_id=str(user_id),
                email=_mask_email(email_str),
            )
        else:
            logger.info(
                "email_verification_generated_dev",
                user_id=str(user_id),
                email=email_str,
                otp=otp,
                ttl=ttl_seconds,
            )

        return EmailVerificationResponse(
            message="Verification code sent to your email",
            email=payload.email,
        )

    async def verify_email(
        self,
        user_id: uuid.UUID,
        payload: VerifyEmailRequest,
    ) -> VerifyEmailResponse:
        """Validate 6-digit OTP and activate email address for user."""
        user = await self._db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(context={"user_id": str(user_id)})

        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user_id)})

        otp_key = f"email_otp:{user_id}"
        pending_key = f"pending_email:{user_id}"
        fail_key = f"email_otp_fails:{user_id}"

        stored_otp = await self._redis.get(otp_key)
        pending_email = await self._redis.get(pending_key)

        if stored_otp is None or pending_email is None:
            raise EmailOTPExpiredError(context={"user_id": str(user_id)})

        if isinstance(stored_otp, bytes):
            stored_otp = stored_otp.decode("utf-8")
        if isinstance(pending_email, bytes):
            pending_email = pending_email.decode("utf-8")

        # Constant-time comparison with brute-force protection
        if not secrets.compare_digest(stored_otp.strip(), payload.otp.strip()):
            current_fails = await self._redis.incr(fail_key)
            ttl_seconds = self._settings.EMAIL_VERIFICATION_EXPIRE_HOURS * 3600
            if current_fails == 1:
                await self._redis.expire(fail_key, ttl_seconds)

            if current_fails >= 5:
                # Lock out and invalidate OTP on 5 failed attempts
                await self._redis.delete(otp_key)
                await self._redis.delete(pending_key)
                await self._redis.delete(fail_key)
                logger.warning(
                    "email_otp_lockout",
                    user_id=str(user_id),
                    attempts=current_fails,
                )
                raise EmailOTPExpiredError(
                    detail="Too many incorrect attempts. Verification code has been invalidated.",
                    context={"user_id": str(user_id)},
                )

            raise InvalidEmailOTPError(
                context={
                    "user_id": str(user_id),
                    "attempts_remaining": max(0, 5 - current_fails),
                }
            )

        # Case-insensitive uniqueness guard before committing
        query = select(User).where(
            func.lower(User.email) == pending_email.lower(), User.id != user_id
        )
        result = await self._db.execute(query)
        if result.scalar_one_or_none() is not None:
            raise EmailAlreadyRegisteredError(context={"email": pending_email})

        verified_at = datetime.now(UTC)
        user.email = pending_email
        user.email_verified_at = verified_at

        try:
            await self._db.commit()
        except IntegrityError as exc:
            await self._db.rollback()
            raise EmailAlreadyRegisteredError(context={"email": pending_email}) from exc

        await self._db.refresh(user)

        # Invalidate Redis verification and attempt tracking keys
        await self._redis.delete(otp_key)
        await self._redis.delete(pending_key)
        await self._redis.delete(fail_key)

        if self._settings.is_production:
            logger.info("email_verified", user_id=str(user.id), email=_mask_email(pending_email))
        else:
            logger.info("email_verified", user_id=str(user.id), email=pending_email)

        return VerifyEmailResponse(
            message="Email verified successfully",
            email=pending_email,
            verified_at=verified_at,
        )
