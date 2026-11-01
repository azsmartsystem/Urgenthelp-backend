import uuid
from typing import Literal

from app.core.validators import NigerianPhone
from app.schemas.base import AppBaseModel
from pydantic import EmailStr, Field, field_validator


class RegisterRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
    email: EmailStr | None = Field(default=None, examples=["user@example.com"])
    full_name: str = Field(min_length=2, max_length=100)
    role: Literal["customer", "helper"]
    password: str = Field(min_length=8, max_length=128)

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


class OTPVerifyRequest(AppBaseModel):
    phone: NigerianPhone = Field(examples=["2348153551975"])
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
    email: EmailStr | None = None
    is_phone_verified: bool = False
    message: str = (
        "Registration successful. Please verify your phone number with the OTP sent to your phone."
    )
