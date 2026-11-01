"""Pydantic schemas for the users module (email verification, profile management)."""

from datetime import datetime

from app.schemas.base import AppBaseModel
from pydantic import EmailStr, Field, field_validator


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
