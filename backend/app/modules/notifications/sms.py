"""SMS gateway integrations — Termii, Twilio, and Mock providers.

Abstracted through the SMSProvider protocol so that switching providers
requires only changing settings (e.g. SMS_PROVIDER="termii" -> "twilio")
without touching business logic or call sites.
"""

from typing import Protocol, cast

import httpx
import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import SMSDispatchError
from app.core.validators import CanonicalPhone

logger = structlog.get_logger(__name__)


class SMSProvider(Protocol):
    """Abstract SMS provider protocol."""

    async def send_sms(self, to: CanonicalPhone, message: str) -> None:
        """Dispatch an SMS to the recipient."""
        ...


class TermiiSMSProvider:
    """Termii SMS provider (standard for Nigerian mobile networks and DND routing)."""

    def __init__(
        self,
        api_key: str,
        sender_id: str = "UrgentHelp",
        base_url: str = "https://api.ng.termii.com",
        channel: str = "dnd",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._sender_id = sender_id
        self._base_url = base_url.rstrip("/")
        self._channel = channel
        self._client = client

    async def send_sms(self, to: CanonicalPhone, message: str) -> None:
        endpoint = f"{self._base_url}/api/sms/send"
        payload: dict[str, str] = {
            "to": str(to),
            "from": self._sender_id,
            "sms": message,
            "type": "plain",
            "channel": self._channel,
            "api_key": self._api_key,
        }

        try:
            if self._client is not None:
                response = await self._client.post(endpoint, json=payload, timeout=10.0)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(endpoint, json=payload)
        except httpx.TimeoutException as exc:
            logger.error("termii_timeout", phone=f"...{to[-4:]}")
            raise SMSDispatchError(
                detail="SMS gateway request timed out. Please try again.",
                context={"provider": "termii", "error": "timeout"},
            ) from exc
        except httpx.RequestError as exc:
            logger.error("termii_request_error", phone=f"...{to[-4:]}", error=str(exc))
            raise SMSDispatchError(
                detail="Unable to reach SMS gateway. Please try again.",
                context={"provider": "termii", "error": str(exc)},
            ) from exc

        try:
            raw_data = response.json()
            data: dict[str, object]
            if isinstance(raw_data, dict):
                data = cast(dict[str, object], raw_data)
            else:
                data = {}
        except Exception:
            data = {}

        if not response.is_success:
            logger.error(
                "termii_dispatch_failed",
                status_code=response.status_code,
                phone=f"...{to[-4:]}",
                response=str(data)[:200],
            )
            raise SMSDispatchError(
                detail="SMS delivery failed via provider.",
                context={"provider": "termii", "status_code": response.status_code},
            )

        # Termii signals errors inside 200 responses occasionally
        response_msg = str(data.get("message", "")).lower()
        if "fail" in response_msg or "error" in response_msg:
            logger.error(
                "termii_dispatch_rejected",
                phone=f"...{to[-4:]}",
                message=str(data.get("message", "")),
            )
            raise SMSDispatchError(
                detail="SMS delivery rejected by provider.",
                context={
                    "provider": "termii",
                    "response_message": str(data.get("message", "")),
                },
            )

        logger.info("termii_sms_dispatched", phone=f"...{to[-4:]}")


class TwilioSMSProvider:
    """Twilio SMS provider for international delivery and fallback."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._client = client

    async def send_sms(self, to: CanonicalPhone, message: str) -> None:
        endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json"
        # Twilio requires full E.164 with the leading '+'
        payload: dict[str, str] = {
            "To": f"+{to}",
            "From": self._from_number,
            "Body": message,
        }

        try:
            if self._client is not None:
                response = await self._client.post(
                    endpoint,
                    data=payload,
                    auth=(self._account_sid, self._auth_token),
                    timeout=10.0,
                )
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        endpoint,
                        data=payload,
                        auth=(self._account_sid, self._auth_token),
                    )
        except httpx.TimeoutException as exc:
            logger.error("twilio_timeout", phone=f"...{to[-4:]}")
            raise SMSDispatchError(
                detail="SMS gateway request timed out. Please try again.",
                context={"provider": "twilio", "error": "timeout"},
            ) from exc
        except httpx.RequestError as exc:
            logger.error("twilio_request_error", phone=f"...{to[-4:]}", error=str(exc))
            raise SMSDispatchError(
                detail="Unable to reach SMS gateway. Please try again.",
                context={"provider": "twilio", "error": str(exc)},
            ) from exc

        if not response.is_success:
            logger.error(
                "twilio_dispatch_failed",
                status_code=response.status_code,
                phone=f"...{to[-4:]}",
            )
            raise SMSDispatchError(
                detail="SMS delivery failed via provider.",
                context={"provider": "twilio", "status_code": response.status_code},
            )

        logger.info("twilio_sms_dispatched", phone=f"...{to[-4:]}")


class MockSMSProvider:
    """Mock SMS provider that records dispatched messages for testing and dev."""

    def __init__(self) -> None:
        self.sent_messages: list[tuple[CanonicalPhone, str]] = []

    async def send_sms(self, to: CanonicalPhone, message: str) -> None:
        self.sent_messages.append((to, message))
        logger.info("mock_sms_dispatched", phone=f"...{to[-4:]}", message=message)


def get_sms_provider(settings: Settings | None = None) -> SMSProvider:
    """Factory to create the configured SMSProvider according to settings."""
    app_settings = settings or get_settings()
    provider_name = app_settings.SMS_PROVIDER.lower()

    if provider_name == "mock":
        return MockSMSProvider()

    if provider_name == "termii":
        if not app_settings.TERMII_API_KEY:
            logger.warning(
                "sms_provider_unconfigured",
                provider="termii",
                detail="TERMII_API_KEY is unset; falling back to MockSMSProvider.",
            )
            return MockSMSProvider()

        return TermiiSMSProvider(
            api_key=app_settings.TERMII_API_KEY,
            sender_id=app_settings.TERMII_SENDER_ID,
            base_url=str(app_settings.TERMII_BASE_URL),
            channel=app_settings.TERMII_CHANNEL,
        )

    if provider_name == "twilio":
        if (
            not app_settings.TWILIO_ACCOUNT_SID
            or not app_settings.TWILIO_AUTH_TOKEN
            or not app_settings.TWILIO_FROM_NUMBER
        ):
            logger.warning(
                "sms_provider_unconfigured",
                provider="twilio",
                detail="Twilio credentials unset; falling back to MockSMSProvider.",
            )
            return MockSMSProvider()

        return TwilioSMSProvider(
            account_sid=app_settings.TWILIO_ACCOUNT_SID,
            auth_token=app_settings.TWILIO_AUTH_TOKEN,
            from_number=app_settings.TWILIO_FROM_NUMBER,
        )

    raise ValueError(f"Unsupported SMS provider: {app_settings.SMS_PROVIDER}")
