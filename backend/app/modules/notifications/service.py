"""Notification service — business logic for dispatching multi-channel notifications."""

import structlog
from app.core.config import Settings, get_settings
from app.core.validators import CanonicalPhone
from app.modules.notifications.sms import SMSProvider, get_sms_provider

logger = structlog.get_logger(__name__)


class NotificationService:
    """Encapsulates notification dispatch workflows (SMS, Push, Email)."""

    def __init__(
        self,
        sms_provider: SMSProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sms_provider = sms_provider or get_sms_provider(self._settings)

    async def send_otp_sms(
        self,
        to: CanonicalPhone,
        otp: str,
        expire_minutes: int,
    ) -> None:
        """Format and dispatch OTP SMS to the recipient."""
        message = f"Your UrgentHelp verification code is {otp}. Valid for {expire_minutes} minutes."
        await self._sms_provider.send_sms(to=to, message=message)
        logger.info("otp_sms_sent", phone=f"...{to[-4:]}")
