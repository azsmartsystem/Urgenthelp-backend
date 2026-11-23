"""Bookings module router — HTTP endpoints for booking lifecycle and matching."""

import uuid
from typing import Annotated

from app.core.exceptions import InvalidTokenError
from app.core.security import TokenPayload, get_current_user
from app.db.session import get_db
from app.modules.bookings.schemas import (
    BookingResponse,
    BookingStatus,
    CancelBookingRequest,
    CreateBookingRequest,
    CreateBookingResponse,
    MatchBookingResponse,
    PaginatedBookingsResponse,
    SelectHelperRequest,
)
from app.modules.bookings.service import BookingService
from app.modules.notifications.push import get_push_provider
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_service(db: Annotated[AsyncSession, Depends(get_db)]) -> BookingService:
    return BookingService(db=db, push_provider=get_push_provider())


def _parse_user_id(current_user: TokenPayload) -> uuid.UUID:
    try:
        return uuid.UUID(current_user.sub)
    except ValueError as exc:
        raise InvalidTokenError(detail="Token subject is not a valid UUID.") from exc


@router.post("", response_model=CreateBookingResponse, status_code=201)
async def create_booking(
    payload: CreateBookingRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> CreateBookingResponse:
    """Create a new booking and return its recommended price range."""
    return await service.create_booking(
        customer_id=_parse_user_id(current_user),
        payload=payload,
    )


@router.get("", response_model=PaginatedBookingsResponse, status_code=200)
async def list_customer_bookings(
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
    status: Annotated[BookingStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PaginatedBookingsResponse:
    """List the authenticated customer's bookings with pagination."""
    return await service.list_customer_bookings(
        customer_id=_parse_user_id(current_user),
        status=status,
        page=page,
        page_size=page_size,
    )


@router.get("/{booking_id}", response_model=BookingResponse, status_code=200)
async def get_booking(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Return booking details for the customer, assigned/offered helper, or admin."""
    return await service.get_booking(
        booking_id=booking_id,
        actor_id=_parse_user_id(current_user),
    )


@router.post("/{booking_id}/match", response_model=MatchBookingResponse, status_code=200)
async def match_helpers(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
    include_busy: Annotated[bool, Query()] = False,
) -> MatchBookingResponse:
    """Run AI matching (auto broadcasts top-3; manual returns ranked candidates)."""
    return await service.match_helpers(
        booking_id=booking_id,
        customer_id=_parse_user_id(current_user),
        include_busy=include_busy,
    )


@router.post("/{booking_id}/select-helper", response_model=BookingResponse, status_code=200)
async def select_helper(
    booking_id: uuid.UUID,
    payload: SelectHelperRequest,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Manually pick a specific available helper and dispatch a targeted offer."""
    return await service.select_helper(
        booking_id=booking_id,
        customer_id=_parse_user_id(current_user),
        helper_id=payload.helper_id,
    )


@router.post("/{booking_id}/accept", response_model=BookingResponse, status_code=200)
async def accept_booking(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Accept a pending booking offer (matched -> accepted)."""
    return await service.accept_booking(
        booking_id=booking_id,
        helper_id=_parse_user_id(current_user),
    )


@router.post("/{booking_id}/decline", response_model=BookingResponse, status_code=200)
async def decline_booking(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Decline a pending offer (reverts matched -> requested if no pending offers remain)."""
    return await service.decline_booking(
        booking_id=booking_id,
        helper_id=_parse_user_id(current_user),
    )


@router.post("/{booking_id}/en-route", response_model=BookingResponse, status_code=200)
async def mark_booking_en_route(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Mark booking as en_route (accepted -> en_route)."""
    return await service.mark_en_route(
        booking_id=booking_id,
        helper_id=_parse_user_id(current_user),
    )


@router.post("/{booking_id}/start", response_model=BookingResponse, status_code=200)
async def start_booking(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Start work on arrival (en_route -> in_progress)."""
    return await service.start_booking(
        booking_id=booking_id,
        helper_id=_parse_user_id(current_user),
    )


@router.post("/{booking_id}/complete", response_model=BookingResponse, status_code=200)
async def complete_booking(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
) -> BookingResponse:
    """Complete a booking (in_progress -> completed)."""
    return await service.complete_booking(
        booking_id=booking_id,
        helper_id=_parse_user_id(current_user),
    )


@router.post("/{booking_id}/cancel", response_model=BookingResponse, status_code=200)
async def cancel_booking(
    booking_id: uuid.UUID,
    current_user: Annotated[TokenPayload, Depends(get_current_user)],
    service: Annotated[BookingService, Depends(_get_service)],
    payload: CancelBookingRequest | None = None,
) -> BookingResponse:
    """Cancel a booking from a cancellable state."""
    return await service.cancel_booking(
        booking_id=booking_id,
        actor_id=_parse_user_id(current_user),
        reason=payload.reason if payload is not None else None,
    )
