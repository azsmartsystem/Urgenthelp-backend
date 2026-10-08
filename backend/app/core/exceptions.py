"""Custom exception hierarchy for UrgentHelp.

Every domain error is a typed exception — never raise raw Exception or
generic HTTPException with a plain string. This makes error handling
consistent, searchable, and safe to expose to clients.

Exception code format: DOMAIN_REASON (e.g. AUTH_INVALID_TOKEN, BOOKING_NOT_FOUND)
"""

from dataclasses import dataclass, field

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette import status

logger = structlog.get_logger(__name__)


# ─── Base ─────────────────────────────────────────────────────────────────────


@dataclass
class UrgentHelpError(Exception):
    """Base exception for all domain errors."""

    code: str  # machine-readable
    detail: str  # human-readable, safe for UI
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    context: dict[str, object] = field(default_factory=lambda: {})

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail}"


# ─── Auth ─────────────────────────────────────────────────────────────────────


@dataclass
class InvalidTokenError(UrgentHelpError):
    code: str = "AUTH_INVALID_TOKEN"
    detail: str = "The provided token is invalid or has expired."
    status_code: int = status.HTTP_401_UNAUTHORIZED


@dataclass
class InvalidCredentialsError(UrgentHelpError):
    code: str = "AUTH_INVALID_CREDENTIALS"
    detail: str = "Phone number or password is incorrect."
    status_code: int = status.HTTP_401_UNAUTHORIZED


@dataclass
class OTPExpiredError(UrgentHelpError):
    code: str = "AUTH_OTP_EXPIRED"
    detail: str = "The OTP has expired. Please request a new one."
    status_code: int = status.HTTP_401_UNAUTHORIZED


@dataclass
class InvalidOTPError(UrgentHelpError):
    code: str = "AUTH_INVALID_OTP"
    detail: str = "The OTP entered is incorrect."
    status_code: int = status.HTTP_400_BAD_REQUEST


@dataclass
class PhoneAlreadyRegisteredError(UrgentHelpError):
    code: str = "AUTH_PHONE_TAKEN"
    detail: str = "An account with this phone number already exists."
    status_code: int = status.HTTP_409_CONFLICT


@dataclass
class EmailAlreadyRegisteredError(UrgentHelpError):
    code: str = "AUTH_EMAIL_TAKEN"
    detail: str = "An account with this email address already exists."
    status_code: int = status.HTTP_409_CONFLICT


@dataclass
class InactiveUserError(UrgentHelpError):
    code: str = "AUTH_USER_INACTIVE"
    detail: str = "This account has been deactivated or suspended."
    status_code: int = status.HTTP_403_FORBIDDEN


# ─── Users / Helpers ──────────────────────────────────────────────────────────


@dataclass
class UserNotFoundError(UrgentHelpError):
    code: str = "USER_NOT_FOUND"
    detail: str = "User not found."
    status_code: int = status.HTTP_404_NOT_FOUND


@dataclass
class HelperNotFoundError(UrgentHelpError):
    code: str = "HELPER_NOT_FOUND"
    detail: str = "Helper not found."
    status_code: int = status.HTTP_404_NOT_FOUND


@dataclass
class HelperNotVerifiedError(UrgentHelpError):
    code: str = "HELPER_NOT_VERIFIED"
    detail: str = "Helper identity verification is pending or incomplete."
    status_code: int = status.HTTP_403_FORBIDDEN


@dataclass
class ForbiddenError(UrgentHelpError):
    code: str = "FORBIDDEN"
    detail: str = "You do not have permission to perform this action."
    status_code: int = status.HTTP_403_FORBIDDEN


# ─── Bookings ─────────────────────────────────────────────────────────────────


@dataclass
class BookingNotFoundError(UrgentHelpError):
    code: str = "BOOKING_NOT_FOUND"
    detail: str = "Booking not found."
    status_code: int = status.HTTP_404_NOT_FOUND


@dataclass
class BookingStateError(UrgentHelpError):
    """Raised when a state transition is invalid (e.g. completing a cancelled booking)."""

    code: str = "BOOKING_INVALID_STATE_TRANSITION"
    detail: str = "This action cannot be performed in the booking's current state."
    status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT


@dataclass
class NoAvailableHelpersError(UrgentHelpError):
    code: str = "BOOKING_NO_HELPERS_AVAILABLE"
    detail: str = "No verified helpers are available in your area for this service."
    status_code: int = status.HTTP_404_NOT_FOUND


# ─── Payments ─────────────────────────────────────────────────────────────────


@dataclass
class PaymentFailedError(UrgentHelpError):
    code: str = "PAYMENT_FAILED"
    detail: str = "Payment could not be processed. Please try again."
    status_code: int = status.HTTP_402_PAYMENT_REQUIRED


@dataclass
class InsufficientWalletBalanceError(UrgentHelpError):
    code: str = "WALLET_INSUFFICIENT_BALANCE"
    detail: str = "Insufficient wallet balance to complete this transaction."
    status_code: int = status.HTTP_402_PAYMENT_REQUIRED


# ─── Categories ───────────────────────────────────────────────────────────────


@dataclass
class CategoryNotFoundError(UrgentHelpError):
    code: str = "CATEGORY_NOT_FOUND"
    detail: str = "Service category not found."
    status_code: int = status.HTTP_404_NOT_FOUND


# ─── External Services ────────────────────────────────────────────────────────


@dataclass
class ExternalServiceError(UrgentHelpError):
    """Raised when a third-party API call fails."""

    code: str = "EXTERNAL_SERVICE_ERROR"
    detail: str = "An external service is temporarily unavailable."
    status_code: int = status.HTTP_502_BAD_GATEWAY


# ─── Global Exception Handler Registration ────────────────────────────────────


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI app."""

    @app.exception_handler(UrgentHelpError)
    async def urgent_help_error_handler(request: Request, exc: UrgentHelpError) -> JSONResponse:
        logger.warning(
            "domain_error",
            code=exc.code,
            detail=exc.detail,
            path=str(request.url),
            context=exc.context,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "detail": exc.detail,
                "path": str(request.url.path),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            error=str(exc),
            exc_info=True,
            path=str(request.url),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": "INTERNAL_ERROR",
                "detail": "An unexpected error occurred. Please try again later.",
                "path": str(request.url.path),
            },
        )
