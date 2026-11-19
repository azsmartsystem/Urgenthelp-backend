"""Shared email verification OTP dispatch, validation, and rollback helpers."""

import secrets
import uuid

import redis.asyncio as aioredis
import structlog
from app.core.config import Settings
from app.core.exceptions import (
    EmailCooldownError,
    EmailOTPExpiredError,
    EmailRateLimitError,
    InvalidEmailOTPError,
)
from app.core.security import generate_otp
from app.modules.notifications.service import NotificationService

logger = structlog.get_logger(__name__)


def mask_email(email: str) -> str:
    """Mask email for privacy in production structured logs."""
    parts = email.split("@", 1)
    if len(parts) != 2:
        return "***"
    name, domain = parts
    masked_name = name[0] + "***" if len(name) > 1 else "***"
    return f"{masked_name}@{domain}"


async def dispatch_email_verification_otp(
    *,
    redis_client: aioredis.Redis,
    settings: Settings,
    notification_service: NotificationService,
    user_id: uuid.UUID,
    email: str,
) -> None:
    """Rate-limit, generate, store, and email a 6-digit verification OTP."""
    email_str = email.strip().lower()
    rate_key = f"email:rate:{user_id}"
    cooldown_key = f"email:cooldown:{user_id}"

    current_count = await redis_client.get(rate_key)
    if current_count is not None and int(current_count) >= settings.EMAIL_MAX_REQUESTS_PER_WINDOW:
        rate_ttl = await redis_client.ttl(rate_key)
        retry_after = max(int(rate_ttl), 1)
        max_req = settings.EMAIL_MAX_REQUESTS_PER_WINDOW
        win_min = settings.EMAIL_RATE_LIMIT_WINDOW_MINUTES
        raise EmailRateLimitError(
            detail=(
                f"Too many email verification requests. Maximum {max_req} "
                f"requests per {win_min} minutes."
            ),
            context={"user_id": str(user_id), "retry_after": retry_after},
        )

    cooldown_acquired = await redis_client.set(
        cooldown_key,
        "1",
        ex=settings.EMAIL_COOLDOWN_SECONDS,
        nx=True,
    )
    if not cooldown_acquired:
        cooldown_ttl = await redis_client.ttl(cooldown_key)
        retry_after = max(int(cooldown_ttl), 1)
        raise EmailCooldownError(
            detail=(
                f"Please wait {retry_after} seconds before requesting "
                "another email verification code."
            ),
            context={"user_id": str(user_id), "retry_after": retry_after},
        )

    current_requests = await redis_client.incr(rate_key)
    rate_window_seconds = settings.EMAIL_RATE_LIMIT_WINDOW_MINUTES * 60
    if current_requests == 1 or await redis_client.ttl(rate_key) == -1:
        await redis_client.expire(rate_key, rate_window_seconds)

    if current_requests > settings.EMAIL_MAX_REQUESTS_PER_WINDOW:
        await redis_client.delete(cooldown_key)
        rate_ttl = await redis_client.ttl(rate_key)
        retry_after = max(int(rate_ttl), 1)
        max_req = settings.EMAIL_MAX_REQUESTS_PER_WINDOW
        win_min = settings.EMAIL_RATE_LIMIT_WINDOW_MINUTES
        raise EmailRateLimitError(
            detail=(
                f"Too many email verification requests. Maximum {max_req} "
                f"requests per {win_min} minutes."
            ),
            context={"user_id": str(user_id), "retry_after": retry_after},
        )

    otp = generate_otp(settings)
    otp_key = f"email_otp:{user_id}"
    pending_key = f"pending_email:{user_id}"
    fail_key = f"email_otp_fails:{user_id}"
    ttl_seconds = settings.EMAIL_VERIFICATION_EXPIRE_HOURS * 3600

    try:
        await redis_client.set(otp_key, otp, ex=ttl_seconds)
        await redis_client.set(pending_key, email_str, ex=ttl_seconds)
        await redis_client.delete(fail_key)
        await notification_service.send_verification_email(
            to=email_str,
            otp=otp,
            expire_hours=settings.EMAIL_VERIFICATION_EXPIRE_HOURS,
        )
    except Exception:
        await rollback_email_verification_otp(redis_client, user_id)
        raise

    if settings.is_production:
        logger.info(
            "email_verification_dispatched",
            user_id=str(user_id),
            email=mask_email(email_str),
        )
    else:
        logger.info(
            "email_verification_generated_dev",
            user_id=str(user_id),
            email=email_str,
            otp=otp,
            ttl=ttl_seconds,
        )


async def rollback_email_verification_otp(
    redis_client: aioredis.Redis,
    user_id: uuid.UUID,
) -> None:
    """Clean up Redis email OTP, cooldown, and rate keys after a failed dispatch or commit."""
    await redis_client.delete(f"email_otp:{user_id}")
    await redis_client.delete(f"pending_email:{user_id}")
    await redis_client.delete(f"email_otp_fails:{user_id}")
    await redis_client.delete(f"email:cooldown:{user_id}")
    rate_key = f"email:rate:{user_id}"
    rate_val = await redis_client.get(rate_key)
    if rate_val is not None and int(rate_val) > 0:
        await redis_client.decr(rate_key)


async def verify_email_verification_otp(
    *,
    redis_client: aioredis.Redis,
    settings: Settings,
    user_id: uuid.UUID,
    submitted_otp: str,
    expected_email: str | None = None,
) -> str:
    """Validate the stored email OTP (with 5-attempt lockout) and return the pending email."""
    otp_key = f"email_otp:{user_id}"
    pending_key = f"pending_email:{user_id}"
    fail_key = f"email_otp_fails:{user_id}"

    stored_otp = await redis_client.get(otp_key)
    pending_email = await redis_client.get(pending_key)

    if stored_otp is None or pending_email is None:
        raise EmailOTPExpiredError(context={"user_id": str(user_id)})

    if isinstance(stored_otp, bytes):
        stored_otp = stored_otp.decode("utf-8")
    if isinstance(pending_email, bytes):
        pending_email = pending_email.decode("utf-8")

    if expected_email is not None and pending_email.lower() != expected_email.strip().lower():
        raise EmailOTPExpiredError(context={"user_id": str(user_id)})

    if not secrets.compare_digest(stored_otp.strip(), submitted_otp.strip()):
        current_fails = await redis_client.incr(fail_key)
        ttl_seconds = settings.EMAIL_VERIFICATION_EXPIRE_HOURS * 3600
        if current_fails == 1:
            await redis_client.expire(fail_key, ttl_seconds)

        if current_fails >= 5:
            await clear_email_verification_otp(redis_client, user_id)
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

    return str(pending_email)


async def clear_email_verification_otp(
    redis_client: aioredis.Redis,
    user_id: uuid.UUID,
) -> None:
    """Delete email OTP, pending email, and attempt counter keys after verification."""
    await redis_client.delete(f"email_otp:{user_id}")
    await redis_client.delete(f"pending_email:{user_id}")
    await redis_client.delete(f"email_otp_fails:{user_id}")
