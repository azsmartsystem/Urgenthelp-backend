"""Root pytest configuration — prevent tests from hitting live SMS/email providers."""

import os


def pytest_configure() -> None:
    """Force mock notification providers during test runs regardless of local .env."""
    os.environ["SMS_PROVIDER"] = "mock"
    os.environ["ENVIRONMENT"] = "test"
