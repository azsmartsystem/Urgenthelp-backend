"""Auth module — registration, OTP login, token refresh."""

from typing import Annotated

import redis.asyncio as aioredis
from app.core.validators import CanonicalPhone
from app.db.redis import get_redis
from app.db.session import get_db
from app.modules.auth.schemas import (
    LoginRequest,
    OTPVerifyRequest,
    RefreshTokenRequest,
    RegisterRequest,
    SendOTPRequest,
    TokenResponse,
)
from app.modules.auth.service import AuthService
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis_client: Annotated[aioredis.Redis, Depends(get_redis)],
) -> AuthService:
    return AuthService(db=db, redis_client=redis_client)


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    service: Annotated[AuthService, Depends(_get_service)],
) -> TokenResponse:
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
    await service.send_otp(CanonicalPhone(payload.phone))


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
