"""Pydantic schemas for the bookings module."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from app.schemas.base import AppBaseModel
from pydantic import ConfigDict, Field, Strict, field_validator, model_validator

BookingStatus = Literal[
    "requested",
    "matched",
    "accepted",
    "en_route",
    "in_progress",
    "completed",
    "cancelled",
    "disputed",
]
BookingUrgency = Literal["standard", "urgent"]
MatchingMode = Literal["auto", "manual"]
OfferStatus = Literal["pending", "accepted", "declined", "superseded", "expired"]
PriceConfidence = Literal["high", "medium", "low"]
BookingSortBy = Literal["scheduled_at", "created_at"]
SortOrder = Literal["asc", "desc"]

SCHEDULED_AT_PAST_GRACE_MINUTES = 5


def _validate_future_scheduled_at(v: datetime) -> datetime:
    """Require timezone-aware scheduled_at, normalize to UTC, and reject past timestamps."""
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware (ISO-8601 with offset or 'Z').")
    normalized = v.astimezone(UTC)
    earliest_allowed = datetime.now(UTC) - timedelta(minutes=SCHEDULED_AT_PAST_GRACE_MINUTES)
    if normalized < earliest_allowed:
        raise ValueError("scheduled_at cannot be in the past.")
    return normalized


class CreateBookingRequest(AppBaseModel):
    """Payload to create a new service booking."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    category_id: Annotated[uuid.UUID, Strict(False)]
    address: str = Field(min_length=5, max_length=500)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    urgency: BookingUrgency = "standard"
    duration_hours: float = Field(default=1.0, gt=0.0, le=24.0)
    scheduled_at: Annotated[datetime | None, Strict(False)] = None
    matching_mode: MatchingMode = "auto"
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        return _validate_future_scheduled_at(v)

    @field_validator("address")
    @classmethod
    def normalize_address(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < 5:
            raise ValueError("address must be at least 5 non-whitespace characters.")
        return stripped

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_coordinates(self) -> Self:
        if self.latitude == 0.0 and self.longitude == 0.0:
            raise ValueError("Coordinates (0.0, 0.0) are not allowed.")
        return self


class SelectHelperRequest(AppBaseModel):
    """Customer manual selection of a specific ranked helper."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    helper_id: Annotated[uuid.UUID, Strict(False)]


class RescheduleBookingRequest(AppBaseModel):
    """Payload to reschedule a booking to a concrete future appointment time."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    scheduled_at: Annotated[datetime, Strict(False)]

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, v: datetime) -> datetime:
        return _validate_future_scheduled_at(v)


class CancelBookingRequest(AppBaseModel):
    """Optional reason when cancelling a booking."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class PriceRangeSchema(AppBaseModel):
    """Recommended price range returned by the pricing engine."""

    minimum: float
    recommended: float
    maximum: float
    currency: str = "NGN"
    confidence: PriceConfidence = "high"


class BookingOfferSummary(AppBaseModel):
    """Summary of an offer sent to a matched helper."""

    id: uuid.UUID
    helper_id: uuid.UUID
    match_score: float
    rank: int
    status: OfferStatus
    responded_at: datetime | None = None


class BookingResponse(AppBaseModel):
    """Full booking representation."""

    id: uuid.UUID
    customer_id: uuid.UUID
    helper_id: uuid.UUID | None = None
    category_id: uuid.UUID
    category_name: str | None = None
    category_slug: str | None = None
    status: BookingStatus
    urgency: BookingUrgency
    duration_hours: float
    scheduled_at: datetime | None = None
    matching_mode: MatchingMode
    address: str
    latitude: float
    longitude: float
    notes: str | None = None
    min_price: float | None = None
    recommended_price: float | None = None
    max_price: float | None = None
    agreed_price: float | None = None
    cancellation_reason: str | None = None
    cancelled_by: uuid.UUID | None = None
    offers: list[BookingOfferSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CreateBookingResponse(BookingResponse):
    """Booking creation response including structured price recommendation."""

    price_range: PriceRangeSchema


class MatchCandidateSchema(AppBaseModel):
    """Helper candidate returned from matching (with busy indicator for manual mode)."""

    helper_id: uuid.UUID
    full_name: str
    profile_photo_url: str | None = None
    is_id_verified: bool
    avg_rating: float
    trust_score: float
    completed_jobs_count: int
    nearest_landmark: str | None = None
    distance_km: float
    match_score: float
    is_busy: bool = False


class MatchBookingResponse(AppBaseModel):
    """Result of running the matching engine for a booking."""

    booking_id: uuid.UUID
    status: BookingStatus
    matching_mode: MatchingMode
    notified_count: int
    candidates: list[MatchCandidateSchema]
    offers: list[BookingOfferSummary] = Field(default_factory=list)


class PaginatedBookingsResponse(AppBaseModel):
    """Paginated list of bookings."""

    items: list[BookingResponse]
    total: int
    page: int
    page_size: int
