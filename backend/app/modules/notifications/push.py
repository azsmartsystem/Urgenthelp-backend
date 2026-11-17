"""Push notification gateway integrations — M1 Protocol and Mock provider."""

from typing import Protocol

import structlog
from app.core.config import Settings, get_settings

logger = structlog.get_logger(__name__)


def _mask_token(token: str) -> str:
    if len(token) >= 8:
        return f"...{token[-4:]}"
    return "***"


class PushProvider(Protocol):
    """Abstract Push notification provider protocol."""

    async def send_push(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        """Dispatch a push notification to a device token."""
        ...


class MockPushProvider:
    """Mock Push provider that records dispatched pushes for testing and development."""

    def __init__(self) -> None:
        self.sent_pushes: list[dict[str, str | dict[str, str] | None]] = []

    async def send_push(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> None:
        self.sent_pushes.append(
            {
                "token": token,
                "title": title,
                "body": body,
                "data": data,
            }
        )
        logger.info("mock_push_dispatched", token=_mask_token(token), title=title)


def get_push_provider(settings: Settings | None = None) -> PushProvider:
    """Factory to create the configured PushProvider according to settings."""
    _ = settings or get_settings()
    return MockPushProvider()
