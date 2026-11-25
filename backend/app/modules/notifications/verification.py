"""Managed OTP verification providers (Twilio Verify v2).

Supports multi-channel OTP delivery and validation across SMS, WhatsApp,
voice call, and email without exposing synchronous SDK calls to async workers.
"""

from typing import Literal, Protocol, cast

import httpx
import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    EmailDispatchError,
    EmailOTPExpiredError,
    EmailRateLimitError,
    OTPExpiredError,
    OTPRateLimitError,
    SMSDispatchError,
)

logger = structlog.get_logger(__name__)

PhoneVerifyChannel = Literal["sms", "whatsapp"]
VerifyChannel = Literal["sms", "whatsapp", "email", "call"]

_EXPIRED_VERIFY_STATUSES = frozenset({"expired", "max_attempts_reached", "deleted"})


class VerificationProvider(Protocol):
    """Abstract provider for managed OTP verification (e.g. Twilio Verify v2)."""

    async def start_verification(self, *, to: str, channel: VerifyChannel) -> None:
        """Initiate an OTP verification via the specified channel."""
        ...

    async def check_verification(
        self,
        *,
        to: str,
        code: str,
        channel: VerifyChannel = "sms",
    ) -> bool:
        """Check a submitted OTP code. Returns True if approved, False if rejected."""
        ...


def _format_target(to: str, channel: VerifyChannel) -> str:
    """Format recipient as lowercase email or strict E.164 phone with leading '+'."""
    stripped = to.strip()
    if channel == "email":
        return stripped.lower()
    return f"+{stripped.lstrip('+')}"


def _redact_target(to: str, channel: VerifyChannel) -> str:
    """Redact recipient identifier before writing structured logs."""
    stripped = to.strip()
    if channel == "email":
        parts = stripped.lower().split("@", 1)
        if len(parts) != 2:
            return "***"
        name, domain = parts
        masked_name = name[0] + "***" if len(name) > 1 else "***"
        return f"{masked_name}@{domain}"
    return f"...{stripped[-4:]}"


def _parse_json_dict(response: httpx.Response) -> dict[str, object]:
    """Safely parse a JSON object response body without introducing Any."""
    try:
        raw = response.json()
        return cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def resolve_twilio_auth(
    *,
    account_sid: str | None = None,
    auth_token: str | None = None,
    api_key_sid: str | None = None,
    api_key_secret: str | None = None,
) -> tuple[str, str] | None:
    """Resolve HTTP Basic Auth credentials, preferring API Key SID/Secret over Auth Token."""
    if api_key_sid and api_key_sid.strip() and api_key_secret and api_key_secret.strip():
        return (api_key_sid.strip(), api_key_secret.strip())
    if account_sid and account_sid.strip() and auth_token and auth_token.strip():
        return (account_sid.strip(), auth_token.strip())
    return None


class TwilioVerifyProvider:
    """Async Twilio Verify v2 provider supporting SMS, WhatsApp, Call, and Email."""

    def __init__(
        self,
        verify_service_sid: str,
        *,
        account_sid: str | None = None,
        auth_token: str | None = None,
        api_key_sid: str | None = None,
        api_key_secret: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        auth = resolve_twilio_auth(
            account_sid=account_sid,
            auth_token=auth_token,
            api_key_sid=api_key_sid,
            api_key_secret=api_key_secret,
        )
        if auth is None:
            raise ValueError(
                "TwilioVerifyProvider requires (api_key_sid, api_key_secret) "
                "or (account_sid, auth_token)."
            )
        self._auth = auth
        self._verify_service_sid = verify_service_sid.strip()
        self._base_url = f"https://verify.twilio.com/v2/Services/{self._verify_service_sid}"
        self._client = client

    async def _post_form(
        self,
        *,
        endpoint: str,
        payload: dict[str, str],
        action: Literal["start", "check"],
        to: str,
        channel: VerifyChannel,
    ) -> httpx.Response:
        redacted = _redact_target(to, channel)
        try:
            if self._client is not None:
                return await self._client.post(
                    endpoint,
                    data=payload,
                    auth=self._auth,
                    timeout=10.0,
                )
            async with httpx.AsyncClient(timeout=10.0) as client:
                return await client.post(
                    endpoint,
                    data=payload,
                    auth=self._auth,
                )
        except httpx.TimeoutException as exc:
            logger.error(f"twilio_verify_{action}_timeout", target=redacted, channel=channel)
            context: dict[str, object] = {
                "provider": "twilio_verify",
                "channel": channel,
                "error": "timeout",
            }
            if channel == "email":
                raise EmailDispatchError(
                    detail="Verification email request timed out. Please try again.",
                    context=context,
                ) from exc
            raise SMSDispatchError(
                detail="Verification gateway request timed out. Please try again.",
                context=context,
            ) from exc
        except httpx.RequestError as exc:
            logger.error(
                f"twilio_verify_{action}_request_error",
                target=redacted,
                channel=channel,
                error=str(exc),
            )
            context = {
                "provider": "twilio_verify",
                "channel": channel,
                "error": str(exc),
            }
            if channel == "email":
                raise EmailDispatchError(
                    detail="Unable to reach email verification gateway. Please try again.",
                    context=context,
                ) from exc
            raise SMSDispatchError(
                detail="Unable to reach verification gateway. Please try again.",
                context=context,
            ) from exc

    async def start_verification(self, *, to: str, channel: VerifyChannel) -> None:
        """Start a Twilio Verify v2 verification via /Verifications."""
        endpoint = f"{self._base_url}/Verifications"
        payload = {
            "To": _format_target(to, channel),
            "Channel": channel,
        }
        redacted = _redact_target(to, channel)

        response = await self._post_form(
            endpoint=endpoint,
            payload=payload,
            action="start",
            to=to,
            channel=channel,
        )

        if not response.is_success:
            error_data = _parse_json_dict(response)
            twilio_code = error_data.get("code")
            twilio_message = str(error_data.get("message", ""))[:200] or None

            logger.error(
                "twilio_verify_start_failed",
                status_code=response.status_code,
                target=redacted,
                channel=channel,
                twilio_code=twilio_code,
                twilio_message=twilio_message,
            )

            if response.status_code == 429 or twilio_code == 60203:
                if channel == "email":
                    raise EmailRateLimitError(
                        context={
                            "provider": "twilio_verify",
                            "channel": channel,
                            "retry_after": 60,
                        }
                    )
                raise OTPRateLimitError(
                    context={
                        "provider": "twilio_verify",
                        "channel": channel,
                        "phone": to[-4:],
                        "retry_after": 60,
                    }
                )

            context: dict[str, object] = {
                "provider": "twilio_verify",
                "channel": channel,
                "status_code": response.status_code,
            }
            if twilio_code is not None:
                context["twilio_code"] = twilio_code

            if channel == "email":
                raise EmailDispatchError(
                    detail="Email verification delivery failed via provider.",
                    context=context,
                )
            raise SMSDispatchError(
                detail="OTP delivery failed via provider.",
                context=context,
            )

        data = _parse_json_dict(response)
        logger.info(
            "twilio_verify_started",
            target=redacted,
            channel=channel,
            status=data.get("status"),
        )

    async def check_verification(
        self,
        *,
        to: str,
        code: str,
        channel: VerifyChannel = "sms",
    ) -> bool:
        """Validate a user-submitted code against Twilio Verify v2 /VerificationCheck."""
        endpoint = f"{self._base_url}/VerificationCheck"
        # Twilio Verify v2 /VerificationCheck accepts To + Code only (no Channel).
        payload = {
            "To": _format_target(to, channel),
            "Code": code.strip(),
        }
        redacted = _redact_target(to, channel)

        response = await self._post_form(
            endpoint=endpoint,
            payload=payload,
            action="check",
            to=to,
            channel=channel,
        )

        if not response.is_success:
            error_data = _parse_json_dict(response)
            twilio_code = error_data.get("code")
            twilio_message = str(error_data.get("message", ""))[:200] or None

            if response.status_code == 404 or twilio_code == 20404:
                logger.info(
                    "twilio_verify_check_expired",
                    target=redacted,
                    channel=channel,
                    twilio_code=twilio_code,
                )
                if channel == "email":
                    raise EmailOTPExpiredError()
                raise OTPExpiredError(context={"phone": to[-4:]})

            if response.status_code == 429 or twilio_code == 60202:
                logger.warning(
                    "twilio_verify_check_max_attempts",
                    target=redacted,
                    channel=channel,
                    twilio_code=twilio_code,
                )
                if channel == "email":
                    raise EmailOTPExpiredError(
                        detail=(
                            "Too many incorrect attempts. Verification code has been invalidated."
                        )
                    )
                raise OTPRateLimitError(
                    detail="Too many incorrect verification attempts. Please request a new code.",
                    context={"phone": to[-4:], "retry_after": 60},
                )

            logger.error(
                "twilio_verify_check_failed",
                status_code=response.status_code,
                target=redacted,
                channel=channel,
                twilio_code=twilio_code,
                twilio_message=twilio_message,
            )
            context: dict[str, object] = {
                "provider": "twilio_verify",
                "channel": channel,
                "status_code": response.status_code,
            }
            if twilio_code is not None:
                context["twilio_code"] = twilio_code

            if channel == "email":
                raise EmailDispatchError(
                    detail="Email verification check failed via provider.",
                    context=context,
                )
            raise SMSDispatchError(
                detail="OTP verification failed via provider.",
                context=context,
            )

        data = _parse_json_dict(response)
        status_val = data.get("status")
        if status_val == "approved":
            logger.info("twilio_verify_approved", target=redacted, channel=channel)
            return True

        if isinstance(status_val, str) and status_val in _EXPIRED_VERIFY_STATUSES:
            logger.info(
                "twilio_verify_check_expired",
                target=redacted,
                channel=channel,
                status=status_val,
            )
            if channel == "email":
                raise EmailOTPExpiredError()
            raise OTPExpiredError(context={"phone": to[-4:]})

        logger.info(
            "twilio_verify_rejected",
            target=redacted,
            channel=channel,
            status=status_val,
        )
        return False


def get_twilio_verify_provider(settings: Settings | None = None) -> TwilioVerifyProvider | None:
    """Create a TwilioVerifyProvider when configured, or None to fall back to Redis OTP."""
    app_settings = settings or get_settings()
    if not app_settings.TWILIO_VERIFY_SERVICE_SID:
        logger.warning(
            "twilio_verify_unconfigured",
            detail="TWILIO_VERIFY_SERVICE_SID unset; falling back to Redis OTP.",
        )
        return None

    auth = resolve_twilio_auth(
        account_sid=app_settings.TWILIO_ACCOUNT_SID,
        auth_token=app_settings.TWILIO_AUTH_TOKEN,
        api_key_sid=app_settings.TWILIO_API_KEY_SID,
        api_key_secret=app_settings.TWILIO_API_KEY_SECRET,
    )
    if auth is None:
        logger.warning(
            "twilio_verify_unconfigured",
            detail="Twilio Verify credentials unset; falling back to Redis OTP.",
        )
        return None

    return TwilioVerifyProvider(
        verify_service_sid=app_settings.TWILIO_VERIFY_SERVICE_SID,
        account_sid=app_settings.TWILIO_ACCOUNT_SID,
        auth_token=app_settings.TWILIO_AUTH_TOKEN,
        api_key_sid=app_settings.TWILIO_API_KEY_SID,
        api_key_secret=app_settings.TWILIO_API_KEY_SECRET,
    )
