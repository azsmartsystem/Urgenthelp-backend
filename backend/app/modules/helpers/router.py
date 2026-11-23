"""Helpers module router — helper profile management and nearby discovery."""

import uuid
from typing import Annotated

from app.core.exceptions import InvalidTokenError
from app.core.security import TokenPayload, get_current_user
from app.db.session import get_db
from app.modules.bookings.schemas import BookingStatus, PaginatedBookingsResponse
from app.modules.bookings.service import BookingService
from app.modules.helpers.schemas import (
    HelperProfileResponse,
    NearbyHelperResponse,
    UpdateHelperProfileRequest,
)
from app.modules.helpers.service import HelperService
from app.modules.notifications.push import get_push_provider
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_service(db: Annotated[AsyncSession, Depends(get_db)]) -> HelperService:
    return HelperService(db=db)


def _get_booking_service(db: Annotated[AsyncSession, Depends(get_db)]) -> BookingService:
    return BookingService(db=db, push_provider=get_push_provider())


def _parse_user_id(current_user: TokenPayload) -> uuid.UUID:
    try:
        return uuid.UUID(current_user.sub)
    except ValueError as exc:
        raise InvalidTokenError(detail="Token subject is not a valid UUID.") from exc


@router.get("/me", response_model=HelperProfileResponse, status_code=200)
async def get_my_helper_profile(
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[HelperService, Depends(_get_service)],
) -> HelperProfileResponse:
    """Return the authenticated helper's profile."""
    return await service.get_my_profile(user_id=_parse_user_id(current_user))


@router.put("/me", response_model=HelperProfileResponse, status_code=200)
async def update_my_helper_profile(
    payload: UpdateHelperProfileRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[HelperService, Depends(_get_service)],
) -> HelperProfileResponse:
    """Update the authenticated helper's skills, radius, availability, bio, and certifications."""
    return await service.update_helper_profile(
        user_id=_parse_user_id(current_user),
        payload=payload,
    )


@router.get("/me/bookings", response_model=PaginatedBookingsResponse, status_code=200)
async def list_my_helper_bookings(
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_booking_service)],
    status: Annotated[BookingStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PaginatedBookingsResponse:
    """List the authenticated helper's job queue (assigned jobs + pending offers)."""
    return await service.list_helper_bookings(
        helper_id=_parse_user_id(current_user),
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/nearby", response_model=list[NearbyHelperResponse], status_code=200)
async def get_helpers_nearby(
    latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
    longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
    service: Annotated[HelperService, Depends(_get_service)],
    radius_km: Annotated[float, Query(gt=0.0, le=50.0)] = 10.0,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[NearbyHelperResponse]:
    """List available helpers near ``(latitude, longitude)`` ordered by distance."""
    if latitude == 0.0 and longitude == 0.0:
        raise HTTPException(
            status_code=422,
            detail="Coordinates (0.0, 0.0) are not allowed.",
        )
    return await service.get_helpers_nearby(
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        category_id=category_id,
        limit=limit,
    )
