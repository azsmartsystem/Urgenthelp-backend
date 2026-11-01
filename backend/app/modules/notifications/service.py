"""Notification service — business logic for dispatching multi-channel notifications."""

import structlog
from app.core.config import Settings, get_settings
from app.core.exceptions import EmailDispatchError
from app.core.validators import CanonicalPhone
from app.modules.notifications.email import EmailProvider, get_email_provider
from app.modules.notifications.sms import SMSProvider, get_sms_provider

logger = structlog.get_logger(__name__)


def _mask_email(email: str) -> str:
    parts = email.split("@", 1)
    if len(parts) != 2:
        return "***"
    name, domain = parts
    masked_name = name[0] + "***" if len(name) > 1 else "***"
    return f"{masked_name}@{domain}"


class NotificationService:
    """Encapsulates notification dispatch workflows (SMS, Push, Email)."""

    def __init__(
        self,
        sms_provider: SMSProvider | None = None,
        email_provider: EmailProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sms_provider = sms_provider or get_sms_provider(self._settings)
        self._email_provider = email_provider or get_email_provider(self._settings)

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

    async def send_verification_email(
        self,
        to: str,
        otp: str,
        expire_hours: int,
    ) -> None:
        """Format and dispatch email verification code to the recipient."""
        subject = "Verify your UrgentHelp email address"
        body = f"Your UrgentHelp verification code is {otp}. Valid for {expire_hours} hours."
        logged_email = _mask_email(to) if self._settings.is_production else to
        try:
            await self._email_provider.send_email(
                to=to,
                subject=subject,
                body=body,
            )
            logger.info("email_verification_sent", email=logged_email)
        except EmailDispatchError:
            raise
        except Exception as exc:
            logger.error("email_dispatch_failed", email=logged_email, error=str(exc))
            raise EmailDispatchError(
                detail="Failed to dispatch verification email. Please try again.",
                context={"email": logged_email, "error": str(exc)},
            ) from exc
