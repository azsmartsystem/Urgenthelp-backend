"""Root pytest configuration — prevent tests from hitting live SMS/email providers."""

import os

from app.core.config import get_settings

os.environ["SMS_PROVIDER"] = "mock"
os.environ["ENVIRONMENT"] = "test"
get_settings.cache_clear()


def pytest_configure() -> None:
    """Force mock notification providers during test runs regardless of local .env."""
    os.environ["SMS_PROVIDER"] = "mock"
    os.environ["ENVIRONMENT"] = "test"
    get_settings.cache_clear()
