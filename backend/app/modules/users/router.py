"""Users module router — email verification and profile management."""

import uuid
from functools import lru_cache
from typing import Annotated, Literal

import redis.asyncio as aioredis
from app.core.config import Settings, get_settings
from app.core.exceptions import InvalidTokenError
from app.core.security import TokenPayload, get_current_user
from app.core.storage import StorageBackend, get_storage_backend
from app.db.redis import get_redis
from app.db.session import get_db
from app.modules.notifications.service import (
    NotificationService,
    get_background_tasks,
    get_notification_service,
)
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
from app.modules.users.service import UserService
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import AnyHttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_notification_service(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession | None, Depends(get_db)] = None,
    background_tasks: Annotated[BackgroundTasks | None, Depends(get_background_tasks)] = None,
) -> NotificationService:
    return get_notification_service(
        db=db,
        settings=settings,
        background_tasks=background_tasks,
    )


@lru_cache
def _cached_storage_backend(
    provider: Literal["s3", "local", "memory"],
    local_dir: str,
    bucket: str | None,
    region: str,
    access_key: str | None,
    secret_key: str | None,
    endpoint_url: str | None,
) -> StorageBackend:
    return get_storage_backend(
        Settings.model_construct(
            STORAGE_PROVIDER=provider,
            LOCAL_STORAGE_DIR=local_dir,
            AWS_S3_BUCKET=bucket,
            AWS_S3_REGION=region,
            AWS_ACCESS_KEY_ID=access_key,
            AWS_SECRET_ACCESS_KEY=secret_key,
            AWS_S3_ENDPOINT_URL=AnyHttpUrl(endpoint_url) if endpoint_url is not None else None,
        )
    )


def _get_storage_backend(
    settings: Annotated[Settings, Depends(get_settings)],
) -> StorageBackend:
    endpoint_url = (
        str(settings.AWS_S3_ENDPOINT_URL) if settings.AWS_S3_ENDPOINT_URL is not None else None
    )
    return _cached_storage_backend(
        settings.STORAGE_PROVIDER,
        settings.LOCAL_STORAGE_DIR,
        settings.AWS_S3_BUCKET,
        settings.AWS_S3_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        endpoint_url,
    )


def _get_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Annotated[aioredis.Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    notification_service: Annotated[NotificationService, Depends(_get_notification_service)],
    storage_backend: Annotated[StorageBackend | None, Depends(_get_storage_backend)] = None,
) -> UserService:
    return UserService(
        db=db,
        redis_client=redis_client,
        settings=settings,
        notification_service=notification_service,
        storage_backend=storage_backend,
    )


def _parse_user_id(current_user: TokenPayload) -> uuid.UUID:
    try:
        return uuid.UUID(current_user.sub)
    except ValueError as exc:
        raise InvalidTokenError(detail="Token subject is not a valid UUID.") from exc


@router.get("/me", response_model=UserProfileResponse, status_code=200)
async def get_me(
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> UserProfileResponse:
    """Return the authenticated user's profile."""
    return await service.get_me(user_id=_parse_user_id(current_user))


@router.put("/me", response_model=UserProfileResponse, status_code=200)
async def update_profile(
    payload: UpdateProfileRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> UserProfileResponse:
    """Partially update the authenticated user's profile."""
    return await service.update_profile(
        user_id=_parse_user_id(current_user),
        payload=payload,
    )


@router.post("/me/photo", response_model=UserProfileResponse, status_code=200)
async def upload_profile_photo(
    file: Annotated[UploadFile, File(...)],
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> UserProfileResponse:
    """Upload and replace the authenticated user's profile photo."""
    return await service.upload_profile_photo(
        user_id=_parse_user_id(current_user),
        file=file,
    )


@router.post("/me/email", response_model=EmailVerificationResponse, status_code=200)
async def request_email_verification(
    payload: UpdateEmailRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> EmailVerificationResponse:
    """Request an email verification OTP for the authenticated user."""
    return await service.request_email_verification(
        user_id=_parse_user_id(current_user),
        payload=payload,
    )


@router.post("/me/email/verify", response_model=VerifyEmailResponse, status_code=200)
async def verify_email(
    payload: VerifyEmailRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> VerifyEmailResponse:
    """Verify an email OTP and activate the email address for the authenticated user."""
    return await service.verify_email(
        user_id=_parse_user_id(current_user),
        payload=payload,
    )


@router.post("/verify-identity", response_model=IdentityVerificationResponse, status_code=201)
async def submit_identity_verification(
    document_type: Annotated[IdentityDocumentType, Form(...)],
    file: Annotated[UploadFile, File(...)],
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> IdentityVerificationResponse:
    """Upload a government ID document (JPEG/PNG/PDF, max 5MB) for admin verification."""
    return await service.submit_identity_verification(
        user_id=_parse_user_id(current_user),
        document_type=document_type,
        file=file,
    )
