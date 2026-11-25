"""Auth module — registration, verification, login, logout, and password recovery."""

from typing import Annotated

import redis.asyncio as aioredis
from app.core.config import Settings, get_settings
from app.core.validators import CanonicalPhone
from app.db.redis import get_redis
from app.db.session import get_db
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    OTPVerifyRequest,
    PhoneCheckRequest,
    PhoneCheckResponse,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    SendEmailVerificationRequest,
    SendOTPRequest,
    TokenResponse,
    VerifyEmailOTPRequest,
)
from app.modules.auth.service import AuthService
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _get_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Annotated[aioredis.Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthService:
    return AuthService(db=db, redis_client=redis_client, settings=settings)


@router.post("/phone/check", response_model=PhoneCheckResponse, status_code=200)
async def check_phone(
    payload: PhoneCheckRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_get_service)],
) -> PhoneCheckResponse:
    """Validate a Nigerian phone number and report whether an account already exists."""
    ip = _get_client_ip(request)
    return await service.check_phone(CanonicalPhone(payload.phone), ip=ip)


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> RegisterResponse:
    """Register a new customer or helper account."""
    return await service.register(payload)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Authenticate user with phone and password."""
    return await service.login(payload)


@router.post("/otp/send", status_code=204)
async def send_otp(
    payload: SendOTPRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> None:
    """Send an OTP to the given phone number."""
    # The schema guarantees payload.phone is already canonical; CanonicalPhone
    # records that for the type checker. This is the single narrowing point
    # between raw user input and the service layer.
    await service.send_otp(CanonicalPhone(payload.phone), channel=payload.channel)


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(
    payload: OTPVerifyRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Verify OTP and return access + refresh tokens."""
    return await service.verify_otp(payload)


@router.post("/email/send", status_code=204)
async def send_email_otp(
    payload: SendEmailVerificationRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> None:
    """Send an email verification OTP to an unverified account."""
    await service.send_email_otp(str(payload.email))


@router.post("/email/verify", response_model=TokenResponse)
async def verify_email_otp(
    payload: VerifyEmailOTPRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Verify email OTP and return access + refresh tokens once phone is also verified."""
    return await service.verify_email_otp(payload)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    payload: RefreshTokenRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Exchange a refresh token for new access + refresh tokens."""
    return await service.refresh(payload.refresh_token)


@router.post("/logout", status_code=204)
async def logout(
    payload: RefreshTokenRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> None:
    """Revoke the provided refresh token to end the current session."""
    await service.logout(payload.refresh_token)


@router.post("/password/forgot", response_model=ForgotPasswordResponse, status_code=200)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_get_service)],
) -> ForgotPasswordResponse:
    """Initiate verified-email password recovery with anti-enumeration."""
    ip = _get_client_ip(request)
    return await service.forgot_password(payload, ip=ip)


@router.post("/password/reset", response_model=ResetPasswordResponse, status_code=200)
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_get_service)],
) -> ResetPasswordResponse:
    """Reset password using a valid one-time email recovery token."""
    ip = _get_client_ip(request)
    return await service.reset_password(payload, ip=ip)
