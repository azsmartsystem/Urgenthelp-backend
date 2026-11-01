"""Email gateway integrations — Mock and production providers.

Abstracted through the EmailProvider protocol so that switching providers
(e.g. Resend, SendGrid, SES) requires only changing configuration
without touching business logic.
"""

from typing import Protocol

import structlog
from app.core.config import Settings, get_settings

logger = structlog.get_logger(__name__)


class EmailProvider(Protocol):
    """Abstract Email provider protocol."""

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> None:
        """Dispatch an email to recipient."""
        ...


class MockEmailProvider:
    """Mock Email provider that records dispatched emails for testing and development."""

    def __init__(self) -> None:
        self.sent_emails: list[dict[str, str | None]] = []

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> None:
        self.sent_emails.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "html_body": html_body,
            }
        )
        logger.info("mock_email_dispatched", to=to, subject=subject)


def get_email_provider(settings: Settings | None = None) -> EmailProvider:
    """Factory to create the configured EmailProvider according to settings."""
    app_settings = settings or get_settings()

    # In dev, test, or when API key is not configured, fall back to MockEmailProvider.
    if not app_settings.EMAIL_PROVIDER_API_KEY or app_settings.ENVIRONMENT in (
        "development",
        "test",
    ):
        return MockEmailProvider()

    # Future concrete provider implementations (Resend/SendGrid/SES) can be instantiated here.
    return MockEmailProvider()
