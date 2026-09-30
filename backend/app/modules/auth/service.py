"""Auth service — business logic for registration, OTP, and token management.

No HTTP awareness here. Receives typed inputs, returns typed outputs,
raises typed exceptions.
"""

import secrets
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    InvalidCredentialsError,
    OTPExpiredError,
    PhoneAlreadyRegisteredError,
)
from app.core.security import create_access_token, create_refresh_token
from app.modules.auth.schemas import (
    OTPVerifyRequest,
    RegisterRequest,
    TokenResponse,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def register(self, payload: RegisterRequest) -> TokenResponse:
        # TODO: check phone uniqueness, hash password, persist user
        raise NotImplementedError

    async def send_otp(self, phone: str) -> None:
        # TODO: generate OTP, store in Redis with TTL, send via SMS provider
        raise NotImplementedError

    async def verify_otp(self, payload: OTPVerifyRequest) -> TokenResponse:
        # TODO: retrieve OTP from Redis, verify, return tokens
        raise NotImplementedError

    async def refresh(self, refresh_token: str) -> TokenResponse:
        # TODO: decode refresh token, issue new access token
        raise NotImplementedError
