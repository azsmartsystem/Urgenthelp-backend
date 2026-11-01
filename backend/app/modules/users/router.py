"""Users module router — email verification and profile management."""

import uuid
from typing import Annotated

import redis.asyncio as aioredis
from app.core.config import Settings, get_settings
from app.core.exceptions import InvalidTokenError
from app.core.security import TokenPayload, get_current_user
from app.db.redis import get_redis
from app.db.session import get_db
from app.modules.notifications.service import NotificationService
from app.modules.users.schemas import (
    EmailVerificationResponse,
    UpdateEmailRequest,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.modules.users.service import UserService
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_notification_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> NotificationService:
    return NotificationService(settings=settings)


def _get_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Annotated[aioredis.Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    notification_service: Annotated[NotificationService, Depends(_get_notification_service)],
) -> UserService:
    return UserService(
        db=db,
        redis_client=redis_client,
        settings=settings,
        notification_service=notification_service,
    )


@router.post("/me/email", response_model=EmailVerificationResponse, status_code=200)
async def request_email_verification(
    payload: UpdateEmailRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> EmailVerificationResponse:
    """Request an email verification OTP for the authenticated user."""
    try:
        user_id = uuid.UUID(current_user.sub)
    except ValueError as exc:
        raise InvalidTokenError(detail="Token subject is not a valid UUID.") from exc

    return await service.request_email_verification(
        user_id=user_id,
        payload=payload,
    )


@router.post("/me/email/verify", response_model=VerifyEmailResponse, status_code=200)
async def verify_email(
    payload: VerifyEmailRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[UserService, Depends(_get_service)],
) -> VerifyEmailResponse:
    """Verify an email OTP and activate the email address for the authenticated user."""
    try:
        user_id = uuid.UUID(current_user.sub)
    except ValueError as exc:
        raise InvalidTokenError(detail="Token subject is not a valid UUID.") from exc

    return await service.verify_email(
        user_id=user_id,
        payload=payload,
    )
