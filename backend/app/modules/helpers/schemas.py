"""Pydantic schemas for the helpers module."""

import uuid
from typing import Annotated, Literal, Self

from app.schemas.base import AppBaseModel
from pydantic import ConfigDict, Field, Strict, field_validator, model_validator

DayOfWeek = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class DaySchedule(AppBaseModel):
    """Single day availability window in 24-hour HH:MM format."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["08:00"])
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["18:00"])

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.start >= self.end:
            raise ValueError("Schedule end time must be strictly after start time.")
        return self


class HelperCategorySummary(AppBaseModel):
    """Compact service category summary embedded in helper responses."""

    id: uuid.UUID
    name: str
    slug: str
    base_price_ngn: float


class HelperProfileResponse(AppBaseModel):
    """Authenticated helper profile including linked categories."""

    id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    profile_photo_url: str | None = None
    is_id_verified: bool
    trust_score: float
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bio: str | None = None
    service_radius_km: float
    is_available: bool
    availability_schedule: dict[DayOfWeek, DaySchedule] | None = None
    certifications: list[str]
    avg_rating: float
    completed_jobs_count: int
    categories: list[HelperCategorySummary]


class UpdateHelperProfileRequest(AppBaseModel):
    """Partial helper profile update payload."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    bio: str | None = Field(default=None, max_length=1000)
    service_radius_km: float | None = Field(default=None, gt=0.0, le=50.0)
    is_available: bool | None = None
    availability_schedule: dict[DayOfWeek, DaySchedule] | None = None
    certifications: list[str] | None = Field(default=None, max_length=20)
    category_ids: list[Annotated[uuid.UUID, Strict(False)]] | None = Field(
        default=None, max_length=20
    )

    @field_validator("bio")
    @classmethod
    def normalize_bio(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("certifications")
    @classmethod
    def validate_certifications(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned: list[str] = []
        for item in v:
            stripped = item.strip()
            if not stripped:
                raise ValueError("Certification entries cannot be blank.")
            if len(stripped) > 120:
                raise ValueError("Each certification must be at most 120 characters.")
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def validate_non_null_required_fields(self) -> Self:
        if "service_radius_km" in self.model_fields_set and self.service_radius_km is None:
            raise ValueError("service_radius_km cannot be null.")
        if "is_available" in self.model_fields_set and self.is_available is None:
            raise ValueError("is_available cannot be null.")
        if "certifications" in self.model_fields_set and self.certifications is None:
            raise ValueError("certifications cannot be null.")
        if "category_ids" in self.model_fields_set and self.category_ids is None:
            raise ValueError("category_ids cannot be null.")
        return self


class NearbyHelperResponse(AppBaseModel):
    """Public nearby-helper search result ordered by distance."""

    helper_id: uuid.UUID
    profile_id: uuid.UUID
    full_name: str
    profile_photo_url: str | None = None
    is_id_verified: bool
    trust_score: float
    avg_rating: float
    completed_jobs_count: int
    bio: str | None = None
    service_radius_km: float
    distance_km: float
    categories: list[HelperCategorySummary]
