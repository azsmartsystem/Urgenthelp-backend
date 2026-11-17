"""Notifications module router — user notification preferences endpoints."""

import uuid
from typing import Annotated

from app.core.exceptions import InvalidTokenError
from app.core.security import TokenPayload, get_current_user
from app.modules.notifications.schemas import (
    PreferencesResponse,
    UpdatePreferencesRequest,
)
from app.modules.notifications.service import NotificationService, get_notification_service
from fastapi import APIRouter, Depends

router = APIRouter()


def _parse_user_id(current_user: TokenPayload) -> uuid.UUID:
    try:
        return uuid.UUID(current_user.sub)
    except ValueError as exc:
        raise InvalidTokenError(detail="Token subject is not a valid UUID.") from exc


@router.get("/preferences", response_model=PreferencesResponse, status_code=200)
async def get_preferences(
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> PreferencesResponse:
    """Return materialized notification preferences for the authenticated user."""
    user_id = _parse_user_id(current_user)
    return await service.get_preferences(user_id=user_id)


@router.put("/preferences", response_model=PreferencesResponse, status_code=200)
async def update_preferences(
    payload: UpdatePreferencesRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[NotificationService, Depends(get_notification_service)],
) -> PreferencesResponse:
    """Partially update (MERGE) notification preferences for the authenticated user."""
    user_id = _parse_user_id(current_user)
    return await service.upsert_preferences(user_id=user_id, payload=payload)
