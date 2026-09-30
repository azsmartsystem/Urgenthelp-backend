"""Auth module — registration, OTP login, token refresh."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.modules.auth.schemas import (
    OTPVerifyRequest,
    RefreshTokenRequest,
    RegisterRequest,
    SendOTPRequest,
    TokenResponse,
)
from app.modules.auth.service import AuthService

router = APIRouter()


def _get_service(db: Annotated[AsyncSession, Depends(get_db)]) -> AuthService:
    return AuthService(db)


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Register a new customer or helper account."""
    return await service.register(payload)


@router.post("/otp/send", status_code=204)
async def send_otp(
    payload: SendOTPRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> None:
    """Send an OTP to the given phone number."""
    await service.send_otp(payload.phone)


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(
    payload: OTPVerifyRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Verify OTP and return access + refresh tokens."""
    return await service.verify_otp(payload)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(
    payload: RefreshTokenRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
    """Exchange a refresh token for new access + refresh tokens."""
    return await service.refresh(payload.refresh_token)
