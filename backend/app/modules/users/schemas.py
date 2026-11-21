"""Pydantic schemas for the users module (email verification, profile management)."""

import uuid
from datetime import datetime
from typing import Literal, Self

from app.core.validators import NigerianPhone
from app.schemas.base import AppBaseModel
from pydantic import ConfigDict, EmailStr, Field, field_validator, model_validator

AddressType = Literal["home", "work", "other"]


class UserProfileResponse(AppBaseModel):
    """Authenticated user profile representation."""

    id: uuid.UUID
    phone: NigerianPhone
    email: EmailStr | None = None
    full_name: str
    role: Literal["customer", "helper", "admin"]
    is_active: bool
    is_id_verified: bool
    is_phone_verified: bool
    email_verified_at: datetime | None = None
    phone_verified_at: datetime | None = None
    trust_score: float
    address: str | None = None
    address_type: AddressType | None = None
    latitude: float | None = None
    longitude: float | None = None
    profile_photo_url: str | None = None


class UpdateProfileRequest(AppBaseModel):
    """Partial profile update payload — immutable fields are forbidden."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        extra="forbid",
    )

    full_name: str | None = Field(default=None, min_length=2, max_length=100)
    address: str | None = Field(default=None, min_length=1, max_length=500)
    address_type: AddressType | None = Field(default=None)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if len(stripped) < 2:
            raise ValueError("Full name must be at least 2 characters.")
        return stripped

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("Address cannot be blank.")
        return stripped

    @field_validator("address_type", mode="before")
    @classmethod
    def normalize_address_type(cls, v: object) -> object:
        if isinstance(v, str):
            lowered = v.strip().lower()
            if lowered in ("other", "others"):
                return "other"
            return lowered
        return v

    @model_validator(mode="after")
    def validate_profile_fields(self) -> Self:
        if "full_name" in self.model_fields_set and self.full_name is None:
            raise ValueError("full_name cannot be null.")

        if "address_type" in self.model_fields_set and self.address_type is None:
            raise ValueError("address_type cannot be null.")

        lat_set = "latitude" in self.model_fields_set
        lon_set = "longitude" in self.model_fields_set
        if lat_set != lon_set:
            raise ValueError("Both latitude and longitude must be provided together.")

        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Both latitude and longitude must be provided together.")

        if self.latitude is not None and self.longitude is not None:
            if self.latitude == 0.0 and self.longitude == 0.0:
                raise ValueError("Coordinates (0.0, 0.0) are not allowed.")

        return self


class UpdateEmailRequest(AppBaseModel):
    email: EmailStr = Field(examples=["user@example.com"])

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class VerifyEmailRequest(AppBaseModel):
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$", examples=["123456"])


class EmailVerificationResponse(AppBaseModel):
    message: str = Field(default="Verification code sent to your email")
    email: EmailStr


class VerifyEmailResponse(AppBaseModel):
    message: str = Field(default="Email verified successfully")
    email: EmailStr
    verified_at: datetime


IdentityDocumentType = Literal[
    "nin",
    "drivers_license",
    "voters_card",
    "international_passport",
]

VerificationStatus = Literal["pending", "approved", "rejected"]


class IdentityVerificationResponse(AppBaseModel):
    """Submitted government ID verification record."""

    id: uuid.UUID
    user_id: uuid.UUID
    document_type: IdentityDocumentType
    status: VerificationStatus
    document_url: str
    created_at: datetime
