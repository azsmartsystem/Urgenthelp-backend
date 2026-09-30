"""JWT auth utilities — token creation, decoding, and FastAPI dependencies."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import ForbiddenError, InvalidTokenError
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


class TokenPayload(BaseModel):
    """Strict shape of the JWT payload."""

    sub: str  # user UUID as string
    role: Literal["customer", "helper", "admin"]
    type: Literal["access", "refresh"]
    iat: datetime
    exp: datetime


def create_access_token(user_id: UUID, role: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_ACCESS_SECRET, algorithm="HS256")


def create_refresh_token(user_id: UUID, role: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.JWT_REFRESH_SECRET, algorithm="HS256")


def decode_token(token: str, secret: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, secret, algorithms=["HS256"])
        return TokenPayload.model_validate(raw)
    except JWTError as exc:
        raise InvalidTokenError() from exc


# ─── FastAPI Dependencies ──────────────────────────────────────────────────────


def _extract_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is None or not credentials.credentials:
        raise InvalidTokenError(detail="Authorization header is missing or malformed.")
    return credentials.credentials


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenPayload:
    token = _extract_token(credentials)
    payload = decode_token(token, settings.JWT_ACCESS_SECRET)
    if payload.type != "access":
        raise InvalidTokenError(detail="Expected an access token.")
    return payload


def require_role(
    *roles: str,
) -> Callable[[TokenPayload], TokenPayload]:
    """Dependency factory — restricts endpoint to specific roles."""

    def _dependency(
        current_user: Annotated[TokenPayload, Depends(get_current_user)],
    ) -> TokenPayload:
        if current_user.role not in roles:
            raise ForbiddenError(
                context={"required_roles": list(roles), "actual_role": current_user.role}
            )
        return current_user

    return _dependency


# Typed convenience aliases
RequireCustomer = Depends(require_role("customer"))
RequireHelper = Depends(require_role("helper"))
RequireAdmin = Depends(require_role("admin"))
RequireAnyUser = Depends(require_role("customer", "helper", "admin"))
