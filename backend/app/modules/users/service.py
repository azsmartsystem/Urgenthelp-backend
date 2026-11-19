"""User service — business logic for user account and email verification lifecycle.

No HTTP awareness here. Receives typed inputs, returns typed outputs,
raises typed exceptions.
"""

import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from app.core.config import Settings, get_settings
from app.core.email_verification import (
    clear_email_verification_otp,
    dispatch_email_verification_otp,
    verify_email_verification_otp,
)
from app.core.email_verification import mask_email as _mask_email
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    StorageError,
    UserNotFoundError,
)
from app.core.storage import (
    MAX_IDENTITY_DOCUMENT_BYTES,
    MAX_PROFILE_PHOTO_BYTES,
    AsyncReadableFile,
    StorageBackend,
    get_storage_backend,
    validate_document_upload,
    validate_image_upload,
)
from app.modules.notifications.service import NotificationService
from app.modules.users.model import IdentityVerification, User
from app.modules.users.schemas import (
    EmailVerificationResponse,
    IdentityDocumentType,
    IdentityVerificationResponse,
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

        await dispatch_email_verification_otp(
            redis_client=self._redis,
            settings=self._settings,
            notification_service=self._notification_service,
            user_id=user_id,
            email=email_str,
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

        pending_email = await verify_email_verification_otp(
            redis_client=self._redis,
            settings=self._settings,
            user_id=user_id,
            submitted_otp=payload.otp,
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
        await clear_email_verification_otp(self._redis, user_id)

        if self._settings.is_production:
            logger.info("email_verified", user_id=str(user.id), email=_mask_email(pending_email))
        else:
            logger.info("email_verified", user_id=str(user.id), email=pending_email)

        return VerifyEmailResponse(
            message="Email verified successfully",
            email=pending_email,
            verified_at=verified_at,
        )

    async def submit_identity_verification(
        self,
        user_id: uuid.UUID,
        document_type: IdentityDocumentType,
        file: AsyncReadableFile,
    ) -> IdentityVerificationResponse:
        """Validate, store a government ID document, and flag the user for admin review."""
        user = await self._get_active_user(user_id)
        validated = await validate_document_upload(file, max_bytes=MAX_IDENTITY_DOCUMENT_BYTES)

        object_key = f"identity-docs/{user_id}/{uuid.uuid4()}.{validated.extension}"
        await self._storage_backend.upload(object_key, validated.data, validated.content_type)

        verification = IdentityVerification(
            user_id=user.id,
            document_type=document_type,
            document_url=object_key,
            status="pending",
        )
        user.is_id_verified = False
        self._db.add(verification)

        try:
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            try:
                await self._storage_backend.delete(object_key)
            except Exception as cleanup_exc:
                logger.warning(
                    "identity_doc_orphan_cleanup_failed",
                    user_id=str(user_id),
                    key=object_key,
                    error=str(cleanup_exc),
                )
            raise

        await self._db.refresh(verification)
        logger.info(
            "identity_verification_submitted",
            user_id=str(user_id),
            verification_id=str(verification.id),
            document_type=document_type,
            content_type=validated.content_type,
            size_bytes=len(validated.data),
        )
        return IdentityVerificationResponse.model_validate(verification)
