"""Unit tests for notification service and SMS providers (Termii, Twilio, Mock)."""

from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import SMSDispatchError
from app.core.validators import CanonicalPhone
from app.modules.notifications.service import NotificationService
from app.modules.notifications.sms import (
    MockSMSProvider,
    TermiiSMSProvider,
    TwilioSMSProvider,
    get_sms_provider,
)

CANONICAL_PHONE = CanonicalPhone("2348153551975")


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def mock_sms_provider() -> MockSMSProvider:
    return MockSMSProvider()


@pytest.fixture
def notification_service(mock_sms_provider: MockSMSProvider) -> NotificationService:
    return NotificationService(sms_provider=mock_sms_provider)


# ─── NotificationService Tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notification_service_send_otp_sms_success(
    notification_service: NotificationService,
    mock_sms_provider: MockSMSProvider,
) -> None:
    await notification_service.send_otp_sms(
        to=CANONICAL_PHONE,
        otp="123456",
        expire_minutes=10,
    )
    assert len(mock_sms_provider.sent_messages) == 1
    phone, msg = mock_sms_provider.sent_messages[0]
    assert phone == CANONICAL_PHONE
    assert "123456" in msg
    assert "10 minutes" in msg


@pytest.mark.asyncio
async def test_notification_service_propagates_provider_error() -> None:
    provider = AsyncMock()
    provider.send_sms.side_effect = SMSDispatchError(detail="Provider down")
    service = NotificationService(sms_provider=provider)

    with pytest.raises(SMSDispatchError):
        await service.send_otp_sms(to=CANONICAL_PHONE, otp="123456", expire_minutes=10)


# ─── TermiiSMSProvider Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_termii_sms_provider_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.ng.termii.com/api/sms/send"
        return httpx.Response(200, json={"message_id": "123", "message": "Successfully Sent"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TermiiSMSProvider(
            api_key="test_api_key",
            sender_id="UrgentHelp",
            client=client,
        )
        await provider.send_sms(CANONICAL_PHONE, "Test message")


@pytest.mark.asyncio
async def test_termii_sms_provider_http_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Insufficient balance"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TermiiSMSProvider(
            api_key="test_api_key",
            sender_id="UrgentHelp",
            client=client,
        )
        with pytest.raises(SMSDispatchError) as exc_info:
            await provider.send_sms(CANONICAL_PHONE, "Test message")
        assert exc_info.value.context.get("status_code") == 400


@pytest.mark.asyncio
async def test_termii_sms_provider_rejection_in_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "Failed to route to telco"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TermiiSMSProvider(
            api_key="test_api_key",
            sender_id="UrgentHelp",
            client=client,
        )
        with pytest.raises(SMSDispatchError) as exc_info:
            await provider.send_sms(CANONICAL_PHONE, "Test message")
        assert "rejected" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_termii_sms_provider_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Timeout connecting to Termii")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TermiiSMSProvider(
            api_key="test_api_key",
            sender_id="UrgentHelp",
            client=client,
        )
        with pytest.raises(SMSDispatchError) as exc_info:
            await provider.send_sms(CANONICAL_PHONE, "Test message")
        assert exc_info.value.context.get("error") == "timeout"


@pytest.mark.asyncio
async def test_termii_sms_provider_request_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TermiiSMSProvider(
            api_key="test_api_key",
            sender_id="UrgentHelp",
            client=client,
        )
        with pytest.raises(SMSDispatchError) as exc_info:
            await provider.send_sms(CANONICAL_PHONE, "Test message")
        assert "unable to reach" in exc_info.value.detail.lower()


# ─── TwilioSMSProvider Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_sms_provider_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Request content is form-urlencoded, so '+' is encoded as '%2B'
        content = request.content.decode("utf-8")
        assert "%2B2348153551975" in content or "+2348153551975" in content
        return httpx.Response(201, json={"sid": "SM123"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TwilioSMSProvider(
            account_sid="AC123",
            auth_token="auth_secret",
            from_number="+1234567890",
            client=client,
        )
        await provider.send_sms(CANONICAL_PHONE, "Test message")


@pytest.mark.asyncio
async def test_twilio_sms_provider_http_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid From number"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TwilioSMSProvider(
            account_sid="AC123",
            auth_token="auth_secret",
            from_number="+1234567890",
            client=client,
        )
        with pytest.raises(SMSDispatchError):
            await provider.send_sms(CANONICAL_PHONE, "Test message")


@pytest.mark.asyncio
async def test_twilio_sms_provider_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Twilio timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = TwilioSMSProvider(
            account_sid="AC123",
            auth_token="auth_secret",
            from_number="+1234567890",
            client=client,
        )
        with pytest.raises(SMSDispatchError) as exc_info:
            await provider.send_sms(CANONICAL_PHONE, "Test message")
        assert exc_info.value.context.get("error") == "timeout"


# ─── Provider Factory Tests ─────────────────────────────────────────────────


def test_get_sms_provider_mock(settings: Settings) -> None:
    s = settings.model_copy(update={"SMS_PROVIDER": "mock"})
    provider = get_sms_provider(s)
    assert isinstance(provider, MockSMSProvider)


def test_get_sms_provider_termii_configured(settings: Settings) -> None:
    s = settings.model_copy(update={"SMS_PROVIDER": "termii", "TERMII_API_KEY": "test_key"})
    provider = get_sms_provider(s)
    assert isinstance(provider, TermiiSMSProvider)


def test_get_sms_provider_termii_unconfigured_falls_back_to_mock(settings: Settings) -> None:
    s = settings.model_copy(update={"SMS_PROVIDER": "termii", "TERMII_API_KEY": None})
    provider = get_sms_provider(s)
    assert isinstance(provider, MockSMSProvider)


def test_get_sms_provider_twilio_configured(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "SMS_PROVIDER": "twilio",
            "TWILIO_ACCOUNT_SID": "AC123",
            "TWILIO_AUTH_TOKEN": "token123",
            "TWILIO_FROM_NUMBER": "+1234567890",
        }
    )
    provider = get_sms_provider(s)
    assert isinstance(provider, TwilioSMSProvider)


def test_get_sms_provider_twilio_unconfigured_falls_back_to_mock(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "SMS_PROVIDER": "twilio",
            "TWILIO_ACCOUNT_SID": None,
        }
    )
    provider = get_sms_provider(s)
    assert isinstance(provider, MockSMSProvider)


def test_get_sms_provider_unsupported(settings: Settings) -> None:
    # Cast to bypass strict type checking when testing bad runtime settings
    s = settings.model_copy(update={"SMS_PROVIDER": cast(object, "unsupported_vendor")})
    with pytest.raises(ValueError):
        get_sms_provider(s)
