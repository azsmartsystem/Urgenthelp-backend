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
    StorageError,
    UserNotFoundError,
)
from app.core.security import generate_otp
from app.core.storage import (
    MAX_PROFILE_PHOTO_BYTES,
    AsyncReadableFile,
    StorageBackend,
    get_storage_backend,
    validate_image_upload,
)
from app.modules.notifications.service import NotificationService
from app.modules.users.model import User
from app.modules.users.schemas import (
    EmailVerificationResponse,
    UpdateEmailRequest,
    UpdateProfileRequest,
    UserProfileResponse,
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


def _is_managed_profile_photo_key(value: str, user_id: uuid.UUID) -> bool:
    """Return True if ``value`` is a managed object key scoped to ``user_id``."""
    return value.startswith(f"public/profile-photos/{user_id}/")


class UserService:
    """Encapsulates all user profile and verification workflows."""

    def __init__(
        self,
        db: AsyncSession,
        redis_client: aioredis.Redis,
        settings: Settings | None = None,
        notification_service: NotificationService | None = None,
        storage_backend: StorageBackend | None = None,
    ) -> None:
        self._db = db
        self._redis = redis_client
        self._settings = settings or get_settings()
        self._notification_service = notification_service or NotificationService(
            settings=self._settings
        )
        self._storage_backend = storage_backend or get_storage_backend(self._settings)

    async def _get_active_user(self, user_id: uuid.UUID) -> User:
        user = await self._db.get(User, user_id)
        if user is None:
            raise UserNotFoundError(context={"user_id": str(user_id)})
        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user_id)})
        return user

    def _to_profile_response(self, user: User) -> UserProfileResponse:
        resolved_photo_url: str | None = None
        raw_photo = (user.profile_photo_url or "").strip()
        if raw_photo:
            if raw_photo.startswith(("https://", "http://", "/")):
                resolved_photo_url = raw_photo
            else:
                try:
                    resolved_photo_url = self._storage_backend.get_public_url(raw_photo)
                except StorageError as exc:
                    logger.warning(
                        "profile_photo_url_resolution_failed",
                        user_id=str(user.id),
                        error=str(exc),
                    )
                    resolved_photo_url = None
        response = UserProfileResponse.model_validate(user)
        if resolved_photo_url != response.profile_photo_url:
            return response.model_copy(update={"profile_photo_url": resolved_photo_url})
        return response

    async def get_me(self, user_id: uuid.UUID) -> UserProfileResponse:
        """Fetch the authenticated active user's profile with resolved photo URL."""
        user = await self._get_active_user(user_id)
        logger.info("user_profile_fetched", user_id=str(user_id))
        return self._to_profile_response(user)

    async def update_profile(
        self,
        user_id: uuid.UUID,
        payload: UpdateProfileRequest,
    ) -> UserProfileResponse:
        """Partially update allowed profile fields for the authenticated active user."""
        user = await self._get_active_user(user_id)
        fields_set = payload.model_fields_set

        if "full_name" in fields_set and payload.full_name is not None:
            user.full_name = payload.full_name
        if "address" in fields_set:
            user.address = payload.address
        if "latitude" in fields_set and "longitude" in fields_set:
            user.latitude = payload.latitude
            user.longitude = payload.longitude

        if fields_set:
            await self._db.commit()
            await self._db.refresh(user)

        logger.info(
            "user_profile_updated",
            user_id=str(user_id),
            updated_fields=sorted(fields_set),
        )
        return self._to_profile_response(user)

    async def upload_profile_photo(
        self,
        user_id: uuid.UUID,
        file: AsyncReadableFile,
    ) -> UserProfileResponse:
        """Validate, upload, and persist a new profile photo for the authenticated user."""
        user = await self._get_active_user(user_id)
        validated = await validate_image_upload(file, max_bytes=MAX_PROFILE_PHOTO_BYTES)

        new_key = f"public/profile-photos/{user_id}/{uuid.uuid4()}.{validated.extension}"
        old_key = user.profile_photo_url

        await self._storage_backend.upload(new_key, validated.data, validated.content_type)

        user.profile_photo_url = new_key
        try:
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            try:
                await self._storage_backend.delete(new_key)
            except Exception as cleanup_exc:
                logger.warning(
                    "profile_photo_orphan_cleanup_failed",
                    user_id=str(user_id),
                    key=new_key,
                    error=str(cleanup_exc),
                )
            raise

        if old_key and old_key != new_key and _is_managed_profile_photo_key(old_key, user_id):
            try:
                await self._storage_backend.delete(old_key)
            except Exception as exc:
                logger.warning(
                    "profile_photo_old_cleanup_failed",
                    user_id=str(user_id),
                    old_key=old_key,
                    error=str(exc),
                )

        await self._db.refresh(user)

        logger.info(
            "profile_photo_uploaded",
            user_id=str(user_id),
            key=new_key,
            content_type=validated.content_type,
            size_bytes=len(validated.data),
        )
        return self._to_profile_response(user)

    async def request_email_verification(
        self,
        user_id: uuid.UUID,
        payload: UpdateEmailRequest,
    ) -> EmailVerificationResponse:
        """Initiate email verification lifecycle for the specified user."""
        await self._get_active_user(user_id)

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
        otp = generate_otp(self._settings)
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
        user = await self._get_active_user(user_id)

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
