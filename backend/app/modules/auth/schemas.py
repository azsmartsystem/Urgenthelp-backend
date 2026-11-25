import uuid
from typing import Literal

from app.core.validators import NigerianPhone
from app.modules.notifications.verification import PhoneVerifyChannel
from app.schemas.base import AppBaseModel
from pydantic import AliasChoices, EmailStr, Field, field_validator


class RegisterRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
    email: EmailStr = Field(examples=["user@example.com"])
    full_name: str = Field(min_length=2, max_length=100)
    role: Literal["customer", "helper"]
    password: str = Field(min_length=8, max_length=128)
    otp_channel: PhoneVerifyChannel | None = Field(
        default=None,
        validation_alias=AliasChoices("otp_channel", "channel"),
        description=(
            "Delivery channel for initial phone OTP ('sms' or 'whatsapp'). "
            "Defaults to server config ('sms')."
        ),
    )

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        return v


class LoginRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
    password: str = Field(min_length=8, max_length=128)


class SendOTPRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
    channel: PhoneVerifyChannel | None = Field(
        default=None,
        validation_alias=AliasChoices("channel", "otp_channel"),
        description=(
            "Delivery channel for OTP ('sms' or 'whatsapp'). Defaults to server config ('sms')."
        ),
    )


class PhoneCheckRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])


class PhoneCheckResponse(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
    is_registered: bool


class OTPVerifyRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
    otp: str = Field(min_length=4, max_length=8)


class SendEmailVerificationRequest(AppBaseModel):
    email: EmailStr = Field(examples=["user@example.com"])


class VerifyEmailOTPRequest(AppBaseModel):
    email: EmailStr = Field(examples=["user@example.com"])
    otp: str = Field(min_length=4, max_length=8)


class RefreshTokenRequest(AppBaseModel):
    refresh_token: str


class TokenResponse(AppBaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"


class RegisterResponse(AppBaseModel):
    id: uuid.UUID
    phone: NigerianPhone
    full_name: str
    role: Literal["customer", "helper", "admin"]
    email: EmailStr
    is_phone_verified: bool = False
    is_email_verified: bool = False
    message: str = "Registration successful. Please verify your phone number and email address."


class ForgotPasswordRequest(AppBaseModel):
    email: EmailStr = Field(examples=["user@example.com"])


class ForgotPasswordResponse(AppBaseModel):
    message: str = (
        "If an account exists with the provided details, "
        "password reset instructions have been sent. "
        "If you need further assistance, please contact support."
    )


class ResetPasswordRequest(AppBaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("token")
    @classmethod
    def strip_token(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Token cannot be blank.")
        return stripped

    @field_validator("new_password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        return v


class ResetPasswordResponse(AppBaseModel):
    message: str = "Password has been reset successfully."
