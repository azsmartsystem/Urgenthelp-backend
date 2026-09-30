"""Auth module Pydantic schemas — strictly typed, no coercion."""

import re
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.base import AppBaseModel


class RegisterRequest(AppBaseModel):
    phone: str = Field(examples=["+2348012345678"])
    full_name: str = Field(min_length=2, max_length=100)
    role: Literal["customer", "helper"]
    password: str = Field(min_length=8, max_length=128)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not re.match(r"^\+?[1-9]\d{7,14}$", v):
            raise ValueError("Invalid phone number format.")
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter.")
        return v


class SendOTPRequest(AppBaseModel):
    phone: str = Field(examples=["+2348012345678"])


class OTPVerifyRequest(AppBaseModel):
    phone: str
    otp: str = Field(min_length=4, max_length=8)


class RefreshTokenRequest(AppBaseModel):
    refresh_token: str


class TokenResponse(AppBaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
