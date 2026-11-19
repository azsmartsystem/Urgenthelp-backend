"""Unit tests for notification service and SMS providers (Termii, Twilio, Mock)."""

import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.core.config import Settings, get_settings
from app.core.exceptions import EmailDispatchError, SMSDispatchError
from app.core.validators import CanonicalPhone
from app.modules.notifications.email import (
    MockEmailProvider,
    get_email_provider,
)
from app.modules.notifications.model import NotificationPreference
from app.modules.notifications.service import NotificationService
from app.modules.notifications.sms import (
    MockSMSProvider,
    TermiiSMSProvider,
    TwilioSMSProvider,
    get_sms_provider,
)
from app.modules.users.model import User

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
        return httpx.Response(
            400,
            json={"code": 21608, "message": "The number +2348153551975 is unverified."},
        )

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
        assert exc_info.value.context.get("status_code") == 400
        assert exc_info.value.context.get("twilio_code") == 21608


@pytest.mark.asyncio
async def test_twilio_sms_provider_http_failure_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

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
        assert exc_info.value.context.get("status_code") == 502
        assert "twilio_code" not in exc_info.value.context


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


# ─── Email Provider Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notification_service_send_verification_email_success() -> None:
    mock_email = MockEmailProvider()
    service = NotificationService(email_provider=mock_email)
    await service.send_verification_email(
        to="user@example.com",
        otp="654321",
        expire_hours=24,
    )
    assert len(mock_email.sent_emails) == 1
    sent = mock_email.sent_emails[0]
    assert sent["to"] == "user@example.com"
    assert "654321" in str(sent["body"])
    assert "24 hours" in str(sent["body"])


@pytest.mark.asyncio
async def test_notification_service_send_verification_email_wraps_exception() -> None:
    failing_provider = AsyncMock()
    failing_provider.send_email.side_effect = RuntimeError("SMTP connection failed")
    service = NotificationService(email_provider=failing_provider)

    with pytest.raises(EmailDispatchError) as exc_info:
        await service.send_verification_email(
            to="user@example.com",
            otp="654321",
            expire_hours=24,
        )
    assert exc_info.value.code == "EMAIL_DISPATCH_FAILED"
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_notification_service_send_verification_email_re_raises_dispatch_error() -> None:
    failing_provider = AsyncMock()
    failing_provider.send_email.side_effect = EmailDispatchError()
    service = NotificationService(email_provider=failing_provider)

    with pytest.raises(EmailDispatchError):
        await service.send_verification_email(
            to="user@example.com",
            otp="654321",
            expire_hours=24,
        )


@pytest.mark.asyncio
async def test_notification_service_send_verification_email_production_logging() -> None:
    from unittest.mock import patch

    with patch.dict("os.environ", {"ENVIRONMENT": "production"}):
        prod_settings = Settings()

    mock_email = MockEmailProvider()
    service = NotificationService(email_provider=mock_email, settings=prod_settings)
    await service.send_verification_email(
        to="user@example.com",
        otp="123456",
        expire_hours=24,
    )
    assert len(mock_email.sent_emails) == 1

    # Also test error logging in production
    failing = AsyncMock()
    failing.send_email.side_effect = RuntimeError("network glitch")
    fail_service = NotificationService(email_provider=failing, settings=prod_settings)
    with pytest.raises(EmailDispatchError):
        await fail_service.send_verification_email(
            to="invalid",
            otp="123456",
            expire_hours=24,
        )


@pytest.mark.asyncio
async def test_notification_service_send_password_reset_email_success() -> None:
    mock_email = MockEmailProvider()
    service = NotificationService(email_provider=mock_email)
    await service.send_password_reset_email(
        to="user@example.com",
        token="test_token_123",
        expire_minutes=30,
    )
    assert len(mock_email.sent_emails) == 1
    sent = mock_email.sent_emails[0]
    assert sent["to"] == "user@example.com"
    assert "test_token_123" in str(sent["body"])
    assert "30 minutes" in str(sent["body"])


@pytest.mark.asyncio
async def test_notification_service_send_password_reset_email_wraps_exception() -> None:
    failing_provider = AsyncMock()
    failing_provider.send_email.side_effect = RuntimeError("SMTP connection failed")
    service = NotificationService(email_provider=failing_provider)

    with pytest.raises(EmailDispatchError) as exc_info:
        await service.send_password_reset_email(
            to="user@example.com",
            token="test_token_123",
            expire_minutes=30,
        )
    assert exc_info.value.code == "EMAIL_DISPATCH_FAILED"
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_notification_service_send_password_reset_email_re_raises_dispatch_error() -> None:
    failing_provider = AsyncMock()
    failing_provider.send_email.side_effect = EmailDispatchError()
    service = NotificationService(email_provider=failing_provider)

    with pytest.raises(EmailDispatchError):
        await service.send_password_reset_email(
            to="user@example.com",
            token="test_token_123",
            expire_minutes=30,
        )


@pytest.mark.asyncio
async def test_notification_service_send_password_reset_email_production_logging() -> None:
    from unittest.mock import patch

    with patch.dict("os.environ", {"ENVIRONMENT": "production"}):
        prod_settings = Settings()

    mock_email = MockEmailProvider()
    service = NotificationService(email_provider=mock_email, settings=prod_settings)
    await service.send_password_reset_email(
        to="user@example.com",
        token="test_token_123",
        expire_minutes=30,
    )
    assert len(mock_email.sent_emails) == 1

    failing = AsyncMock()
    failing.send_email.side_effect = RuntimeError("network glitch")
    fail_service = NotificationService(email_provider=failing, settings=prod_settings)
    with pytest.raises(EmailDispatchError):
        await fail_service.send_password_reset_email(
            to="invalid",
            token="test_token_123",
            expire_minutes=30,
        )


@pytest.mark.asyncio
async def test_notification_service_send_password_reset_sms_success() -> None:
    mock_sms = AsyncMock()
    service = NotificationService(sms_provider=mock_sms)
    await service.send_password_reset_sms(
        to=CanonicalPhone("2348012345678"),
        otp="654321",
        expire_minutes=30,
    )
    mock_sms.send_sms.assert_awaited_once()
    assert "654321" in mock_sms.send_sms.call_args.kwargs["message"]
    assert "30 minutes" in mock_sms.send_sms.call_args.kwargs["message"]


def test_get_email_provider(settings: Settings) -> None:
    provider = get_email_provider(settings)
    assert isinstance(provider, MockEmailProvider)

    # In dev with no api key
    s_dev = settings.model_copy(
        update={"EMAIL_PROVIDER_API_KEY": None, "ENVIRONMENT": "development"}
    )
    assert isinstance(get_email_provider(s_dev), MockEmailProvider)

    # In prod with api key
    s_prod = settings.model_copy(
        update={"EMAIL_PROVIDER_API_KEY": "resend_123", "ENVIRONMENT": "production"}
    )
    assert isinstance(get_email_provider(s_prod), MockEmailProvider)


# ─── Registry, PushProvider, InlineDispatcher & Preference Tests ─────────────


def _make_mock_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.begin_nested = MagicMock(return_value=AsyncMock())
    return db


def _make_user(
    user_id: uuid.UUID,
    *,
    is_active: bool = True,
    email: str | None = "user@example.com",
    email_verified_at: datetime | None = datetime(2026, 1, 1, tzinfo=UTC),
    phone: str = "2348012345678",
    phone_verified_at: datetime | None = datetime(2026, 1, 1, tzinfo=UTC),
    fcm_token: str | None = None,
) -> User:
    user = User(
        id=user_id,
        phone=phone,
        email=email,
        full_name="Test User",
        hashed_password="hashed_password",
        role="customer",
        is_active=is_active,
        is_id_verified=False,
        is_phone_verified=phone_verified_at is not None,
        email_verified_at=email_verified_at,
        phone_verified_at=phone_verified_at,
        trust_score=50.0,
    )
    if fcm_token is not None:
        user.__dict__["fcm_token"] = fcm_token
    return user


def _scalars_result(rows: list[NotificationPreference]) -> MagicMock:
    scalars = MagicMock()
    scalars.all.return_value = rows
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _scalar_one_or_none_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def test_registry_constants_and_helpers() -> None:
    from app.core.exceptions import (
        InvalidNotificationChannelError,
        InvalidNotificationEventError,
    )
    from app.modules.notifications.registry import (
        DEFAULT_PREFERENCES,
        EVENT_REGISTRY,
        VALID_CHANNELS,
        EventClass,
        get_event_class,
        validate_channel,
    )

    assert VALID_CHANNELS == ("push", "email", "sms")
    assert len(EVENT_REGISTRY) == 10
    assert len(DEFAULT_PREFERENCES) == len(VALID_CHANNELS) * len(EVENT_REGISTRY)

    for (channel, event_type), default_enabled in DEFAULT_PREFERENCES.items():
        if EVENT_REGISTRY[event_type] == EventClass.PREFERENTIAL and channel == "sms":
            assert default_enabled is False
        else:
            assert default_enabled is True

    assert validate_channel("email") == "email"
    with pytest.raises(InvalidNotificationChannelError):
        validate_channel("in_app")

    assert get_event_class("password.reset") == EventClass.TRANSACTIONAL
    assert get_event_class("account.deleted") == EventClass.SYSTEM_CRITICAL
    assert get_event_class("booking.matched") == EventClass.PREFERENTIAL
    with pytest.raises(InvalidNotificationEventError):
        get_event_class("unknown.event")


@pytest.mark.asyncio
async def test_mock_push_provider_and_masking(settings: Settings) -> None:
    from app.modules.notifications.push import MockPushProvider, get_push_provider

    provider = get_push_provider(settings)
    assert isinstance(provider, MockPushProvider)

    await provider.send_push(
        token="fcm_secret_token_abcd",
        title="Booking Match",
        body="Helper found",
        data={"booking_id": "123"},
    )
    await provider.send_push(token="short", title="Short", body="Body")
    assert len(provider.sent_pushes) == 2
    assert provider.sent_pushes[0]["title"] == "Booking Match"


def test_masking_helpers() -> None:
    from app.modules.notifications.dispatcher import mask_email, mask_recipient

    assert mask_email("john@example.com") == "j***@example.com"
    assert mask_email("a@example.com") == "***@example.com"
    assert mask_email("invalid") == "***"

    assert mask_recipient("email", "john@example.com") == "j***@example.com"
    assert mask_recipient("sms", "2348012345678") == "...5678"
    assert mask_recipient("sms", "12") == "***"
    assert mask_recipient("push", "fcm_token_value_1234") == "...1234"
    assert mask_recipient("push", "short") == "***"
    assert mask_recipient("unknown", "abc") == "***"


@pytest.mark.asyncio
async def test_inline_dispatcher_transactional_channels_and_errors() -> None:
    from app.core.exceptions import (
        InvalidNotificationChannelError,
        PushDispatchError,
    )
    from app.modules.notifications.dispatcher import DispatchPayload, InlineDispatcher
    from app.modules.notifications.push import MockPushProvider
    from app.modules.notifications.registry import EventClass
    from app.modules.notifications.schemas import NotificationContent

    mock_sms = MockSMSProvider()
    mock_email = MockEmailProvider()
    mock_push = MockPushProvider()
    dispatcher = InlineDispatcher(
        sms_provider=mock_sms,
        email_provider=mock_email,
        push_provider=mock_push,
    )
    uid = uuid.uuid4()

    res_sms = await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="sms",
            event_type="otp.sent",
            event_class=EventClass.TRANSACTIONAL,
            recipient="2348012345678",
            content=NotificationContent(body="Code 1234"),
        )
    )
    assert res_sms.status == "sent"
    assert mock_sms.sent_messages == [(CanonicalPhone("2348012345678"), "Code 1234")]

    res_email = await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="email",
            event_type="account.deleted",
            event_class=EventClass.SYSTEM_CRITICAL,
            recipient="user@example.com",
            content=NotificationContent(
                subject="Account Deleted",
                body="Confirmed",
                html_body="<p>Confirmed</p>",
            ),
        )
    )
    assert res_email.status == "sent"
    assert mock_email.sent_emails[0] == {
        "to": "user@example.com",
        "subject": "Account Deleted",
        "body": "Confirmed",
        "html_body": "<p>Confirmed</p>",
    }

    # Email subject fallbacks: title -> default
    await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="email",
            event_type="password.reset",
            event_class=EventClass.TRANSACTIONAL,
            recipient="user@example.com",
            content=NotificationContent(title="Title Fallback", body="Reset body"),
        )
    )
    await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="email",
            event_type="password.reset",
            event_class=EventClass.TRANSACTIONAL,
            recipient="user@example.com",
            content=NotificationContent(body="Plain body"),
        )
    )
    assert mock_email.sent_emails[1]["subject"] == "Title Fallback"
    assert mock_email.sent_emails[2]["subject"] == "UrgentHelp Notification"

    res_push = await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="push",
            event_type="security.alert",
            event_class=EventClass.TRANSACTIONAL,
            recipient="fcm_secret_token_9999",
            content=NotificationContent(title="Alert", body="New login", data={"ip": "1.2.3.4"}),
        )
    )
    assert res_push.status == "sent"
    assert mock_push.sent_pushes[0] == {
        "token": "fcm_secret_token_9999",
        "title": "Alert",
        "body": "New login",
        "data": {"ip": "1.2.3.4"},
    }

    # Push title fallbacks: subject -> default
    await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="push",
            event_type="security.alert",
            event_class=EventClass.TRANSACTIONAL,
            recipient="fcm_secret_token_9999",
            content=NotificationContent(subject="Subject Fallback", body="Alert body"),
        )
    )
    await dispatcher.dispatch(
        DispatchPayload(
            user_id=uid,
            channel="push",
            event_type="security.alert",
            event_class=EventClass.TRANSACTIONAL,
            recipient="fcm_secret_token_9999",
            content=NotificationContent(body="Default title body"),
        )
    )
    assert mock_push.sent_pushes[1]["title"] == "Subject Fallback"
    assert mock_push.sent_pushes[2]["title"] == "UrgentHelp"

    # Unexpected error on push wraps in PushDispatchError (502)
    failing_push = AsyncMock()
    failing_push.send_push.side_effect = RuntimeError("FCM outage")
    failing_sms = AsyncMock()
    failing_sms.send_sms.side_effect = RuntimeError("Termii down")
    failing_email = AsyncMock()
    failing_email.send_email.side_effect = EmailDispatchError()

    err_dispatcher = InlineDispatcher(
        sms_provider=failing_sms,
        email_provider=failing_email,
        push_provider=failing_push,
    )
    with pytest.raises(PushDispatchError):
        await err_dispatcher.dispatch(
            DispatchPayload(
                user_id=uid,
                channel="push",
                event_type="security.alert",
                event_class=EventClass.TRANSACTIONAL,
                recipient="fcm_secret_token_9999",
                content=NotificationContent(body="Alert"),
            )
        )
    with pytest.raises(SMSDispatchError):
        await err_dispatcher.dispatch(
            DispatchPayload(
                user_id=uid,
                channel="sms",
                event_type="otp.sent",
                event_class=EventClass.TRANSACTIONAL,
                recipient="2348012345678",
                content=NotificationContent(body="OTP"),
            )
        )
    with pytest.raises(EmailDispatchError):
        await err_dispatcher.dispatch(
            DispatchPayload(
                user_id=uid,
                channel="email",
                event_type="password.reset",
                event_class=EventClass.TRANSACTIONAL,
                recipient="user@example.com",
                content=NotificationContent(body="Reset"),
            )
        )
    with pytest.raises(InvalidNotificationChannelError):
        await err_dispatcher.dispatch(
            DispatchPayload(
                user_id=uid,
                channel="in_app",
                event_type="password.reset",
                event_class=EventClass.TRANSACTIONAL,
                recipient="user@example.com",
                content=NotificationContent(body="Reset"),
            )
        )
    assert err_dispatcher._wrap_provider_error("email", RuntimeError("x")).status_code == 502
    assert err_dispatcher._wrap_provider_error("other", RuntimeError("x")).status_code == 502


@pytest.mark.asyncio
async def test_inline_dispatcher_preferential_background_and_fallback_with_sentry() -> None:
    from unittest.mock import patch

    from app.core.exceptions import InvalidNotificationChannelError
    from app.modules.notifications.dispatcher import DispatchPayload, InlineDispatcher
    from app.modules.notifications.push import MockPushProvider
    from app.modules.notifications.registry import EventClass
    from app.modules.notifications.schemas import NotificationContent
    from fastapi import BackgroundTasks

    uid = uuid.uuid4()
    payload = DispatchPayload(
        user_id=uid,
        channel="email",
        event_type="booking.matched",
        event_class=EventClass.PREFERENTIAL,
        recipient="user@example.com",
        content=NotificationContent(subject="Match", body="Helper matched"),
    )

    # BackgroundTasks queued mode rejects invalid channel before enqueueing
    bg = BackgroundTasks()
    mock_email = MockEmailProvider()
    queued_dispatcher = InlineDispatcher(
        sms_provider=MockSMSProvider(),
        email_provider=mock_email,
        push_provider=MockPushProvider(),
        background_tasks=bg,
    )
    with pytest.raises(InvalidNotificationChannelError):
        await queued_dispatcher.dispatch(
            DispatchPayload(
                user_id=uid,
                channel="in_app",
                event_type="booking.matched",
                event_class=EventClass.PREFERENTIAL,
                recipient="user@example.com",
                content=NotificationContent(body="Invalid channel"),
            )
        )
    assert len(bg.tasks) == 0

    queued_res = await queued_dispatcher.dispatch(payload)
    assert queued_res.status == "queued"
    assert len(bg.tasks) == 1
    await bg()
    assert len(mock_email.sent_emails) == 1

    # Fallback mode (background_tasks=None) succeeds
    fallback_ok = InlineDispatcher(
        sms_provider=MockSMSProvider(),
        email_provider=MockEmailProvider(),
        push_provider=MockPushProvider(),
        background_tasks=None,
    )
    ok_res = await fallback_ok.dispatch(payload)
    assert ok_res.status == "sent"

    # Fallback mode swallows exception, returns DeliveryResult(status="failed"),
    # and calls sentry_sdk.capture_exception only when is_initialized() is True.
    failing_email = AsyncMock()
    failing_email.send_email.side_effect = RuntimeError("SMTP down")
    fallback_fail = InlineDispatcher(
        sms_provider=MockSMSProvider(),
        email_provider=failing_email,
        push_provider=MockPushProvider(),
        background_tasks=None,
    )

    with (
        patch("app.modules.notifications.dispatcher.sentry_sdk.is_initialized", return_value=False),
        patch("app.modules.notifications.dispatcher.sentry_sdk.capture_exception") as mock_cap,
    ):
        fail_res = await fallback_fail.dispatch(payload)
        assert fail_res.status == "failed"
        mock_cap.assert_not_called()

    with (
        patch("app.modules.notifications.dispatcher.sentry_sdk.is_initialized", return_value=True),
        patch("app.modules.notifications.dispatcher.sentry_sdk.capture_exception") as mock_cap,
    ):
        fail_res_2 = await fallback_fail.dispatch(payload)
        assert fail_res_2.status == "failed"
        mock_cap.assert_called_once()


@pytest.mark.asyncio
async def test_notification_service_get_and_upsert_preferences() -> None:
    from app.core.exceptions import (
        InactiveUserError,
        InvalidNotificationChannelError,
        InvalidNotificationEventError,
        TransactionalOptOutError,
        UrgentHelpError,
        UserNotFoundError,
    )
    from app.modules.notifications.model import NotificationPreference
    from app.modules.notifications.registry import EVENT_REGISTRY, VALID_CHANNELS, EventClass
    from app.modules.notifications.schemas import PreferenceItem, UpdatePreferencesRequest

    uid = uuid.uuid4()

    # Missing DB raises UrgentHelpError
    no_db_service = NotificationService()
    with pytest.raises(UrgentHelpError) as exc_info:
        await no_db_service.get_preferences(uid)
    assert exc_info.value.code == "NOTIFICATION_DB_REQUIRED"

    # Missing user -> 404; inactive user -> 403
    db = _make_mock_db()
    service = NotificationService(db=db)

    db.get.return_value = None
    with pytest.raises(UserNotFoundError):
        await service.get_preferences(uid)

    db.get.return_value = _make_user(uid, is_active=False)
    with pytest.raises(InactiveUserError):
        await service.get_preferences(uid)

    # Empty state -> 30 defaults, 0 writes, and SELECT uses populate_existing=True
    db.get.return_value = _make_user(uid, is_active=True)
    db.execute.return_value = _scalars_result([])
    empty_prefs = await service.get_preferences(uid)
    assert len(empty_prefs.preferences) == len(VALID_CHANNELS) * len(EVENT_REGISTRY)
    executed_stmt = db.execute.call_args.args[0]
    assert executed_stmt.get_execution_options().get("populate_existing") is True
    db.commit.assert_not_awaited()
    sms_match = next(
        p
        for p in empty_prefs.preferences
        if p.channel == "sms" and p.event_type == "booking.matched"
    )
    assert sms_match.enabled is False
    email_match = next(
        p
        for p in empty_prefs.preferences
        if p.channel == "email" and p.event_type == "booking.matched"
    )
    assert email_match.enabled is True

    # Overrides applied
    override_row = NotificationPreference(
        id=uuid.uuid4(),
        user_id=uid,
        channel="email",
        event_type="booking.matched",
        enabled=False,
    )
    db.execute.return_value = _scalars_result([override_row])
    overridden = await service.get_preferences(uid)
    overridden_item = next(
        p
        for p in overridden.preferences
        if p.channel == "email" and p.event_type == "booking.matched"
    )
    assert overridden_item.enabled is False

    # Validation errors on upsert: zero writes
    db.execute.reset_mock()
    db.commit.reset_mock()
    with pytest.raises(InvalidNotificationChannelError):
        await service.upsert_preferences(
            uid,
            UpdatePreferencesRequest(
                preferences=[
                    PreferenceItem(channel="email", event_type="booking.matched", enabled=False),
                    PreferenceItem(channel="in_app", event_type="booking.matched", enabled=True),
                ]
            ),
        )
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()

    with pytest.raises(InvalidNotificationEventError):
        await service.upsert_preferences(
            uid,
            UpdatePreferencesRequest(
                preferences=[PreferenceItem(channel="email", event_type="bad.event", enabled=False)]
            ),
        )
    db.execute.assert_not_awaited()

    for critical_event in ("password.reset", "account.deleted"):
        with pytest.raises(TransactionalOptOutError):
            await service.upsert_preferences(
                uid,
                UpdatePreferencesRequest(
                    preferences=[
                        PreferenceItem(channel="email", event_type=critical_event, enabled=False)
                    ]
                ),
            )
    db.execute.assert_not_awaited()

    # Transactional enabled=True is a no-op (zero writes)
    db.execute.return_value = _scalars_result([])
    noop_res = await service.upsert_preferences(
        uid,
        UpdatePreferencesRequest(
            preferences=[
                PreferenceItem(channel="email", event_type="password.reset", enabled=True),
                PreferenceItem(channel="push", event_type="terms.updated", enabled=True),
            ]
        ),
    )
    assert len(noop_res.preferences) == len(VALID_CHANNELS) * len(EVENT_REGISTRY)
    db.commit.assert_not_awaited()

    # Duplicate items resolve last-write-wins and commit once
    db.execute.reset_mock()
    db.execute.side_effect = [MagicMock(), _scalars_result([])]
    await service.upsert_preferences(
        uid,
        UpdatePreferencesRequest(
            preferences=[
                PreferenceItem(channel="sms", event_type="booking.matched", enabled=True),
                PreferenceItem(channel="sms", event_type="booking.matched", enabled=False),
            ]
        ),
    )
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()

    # DB failure during upsert rolls back
    db.execute.side_effect = RuntimeError("DB write error")
    with pytest.raises(RuntimeError):
        await service.upsert_preferences(
            uid,
            UpdatePreferencesRequest(
                preferences=[
                    PreferenceItem(channel="email", event_type="booking.matched", enabled=False)
                ]
            ),
        )
    db.rollback.assert_awaited_once()
    assert EventClass.PREFERENTIAL.value == "preferential"


@pytest.mark.asyncio
async def test_notification_service_check_preference_savepoint_and_degradation() -> None:
    uid = uuid.uuid4()

    # Without DB: transactional fails open, preferential fails closed
    no_db = NotificationService()
    assert await no_db._check_preference(uid, "email", "password.reset") is True
    assert await no_db._check_preference(uid, "email", "booking.matched") is False

    # With DB: stored override & default fallback
    db = _make_mock_db()
    service = NotificationService(db=db)

    db.execute.return_value = _scalar_one_or_none_result(False)
    assert await service._check_preference(uid, "email", "booking.matched") is False
    db.begin_nested.assert_called()

    db.execute.return_value = _scalar_one_or_none_result(None)
    assert await service._check_preference(uid, "sms", "booking.matched") is False
    assert await service._check_preference(uid, "email", "booking.matched") is True
    assert await service._check_preference(uid, "email", "password.reset") is True

    # Savepoint DB error: preferential fails closed, transactional/system_critical fail open
    db.execute.side_effect = RuntimeError("connection dropped")
    assert await service._check_preference(uid, "email", "booking.matched") is False
    assert await service._check_preference(uid, "email", "password.reset") is True
    assert await service._check_preference(uid, "sms", "account.deleted") is True


@pytest.mark.asyncio
async def test_notification_service_resolve_contact_and_send_notification() -> None:
    from app.modules.notifications.dispatcher import InlineDispatcher
    from app.modules.notifications.push import MockPushProvider
    from app.modules.notifications.schemas import NotificationContent
    from app.modules.notifications.service import NoOpPreferenceSink, get_notification_service
    from fastapi import BackgroundTasks

    uid = uuid.uuid4()
    content = NotificationContent(subject="Update", title="Update", body="Body text")

    # NoOpPreferenceSink
    sink = NoOpPreferenceSink()
    await sink.mark_channel_suppressed(uid, "email", "hard_bounce")

    # DI helper works without args and with explicit args
    di_service = get_notification_service()
    assert isinstance(di_service, NotificationService)

    # No DB resolve_contact -> None
    assert await NotificationService().resolve_contact(uid, "email") is None

    db = _make_mock_db()
    mock_email = MockEmailProvider()
    mock_sms = MockSMSProvider()
    mock_push = MockPushProvider()
    bg = BackgroundTasks()
    dispatcher = InlineDispatcher(
        sms_provider=mock_sms,
        email_provider=mock_email,
        push_provider=mock_push,
        background_tasks=bg,
    )
    service = NotificationService(
        sms_provider=mock_sms,
        email_provider=mock_email,
        push_provider=mock_push,
        db=db,
        dispatcher=dispatcher,
    )

    # 1. Opted out preferential -> suppressed
    db.execute.return_value = _scalar_one_or_none_result(False)
    res_opt_out = await service.send_notification(uid, "email", "booking.matched", content)
    assert res_opt_out.status == "suppressed"
    assert res_opt_out.reason == "preference_disabled"

    # 2. Missing / inactive / unverified / no_device_token -> suppressed
    db.execute.return_value = _scalar_one_or_none_result(True)
    db.get.return_value = None
    assert (await service.send_notification(uid, "email", "booking.matched", content)).status == (
        "suppressed"
    )

    db.get.return_value = _make_user(uid, is_active=False)
    assert (await service.send_notification(uid, "email", "booking.matched", content)).status == (
        "suppressed"
    )

    db.get.return_value = _make_user(uid, email=None, email_verified_at=None)
    assert await service.resolve_contact(uid, "email") is None

    db.get.return_value = _make_user(
        uid, email="", email_verified_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    res_empty_email = await service.send_notification(uid, "email", "booking.matched", content)
    assert res_empty_email.status == "suppressed"
    assert res_empty_email.reason == "missing_email"

    db.get.side_effect = RuntimeError("User DB failure")
    res_user_lookup_err = await service.send_notification(uid, "email", "booking.matched", content)
    assert res_user_lookup_err.status == "suppressed"
    assert res_user_lookup_err.reason == "db_lookup_failed"
    db.get.side_effect = None

    db.get.return_value = _make_user(uid, email="u@example.com", email_verified_at=None)
    res_unverified_email = await service.send_notification(uid, "email", "booking.matched", content)
    assert res_unverified_email.status == "suppressed"
    assert res_unverified_email.reason == "email_unverified"

    db.get.return_value = _make_user(uid, phone="", phone_verified_at=None)
    assert await service.resolve_contact(uid, "sms") is None

    db.get.return_value = _make_user(uid, phone_verified_at=None)
    res_unverified_sms = await service.send_notification(uid, "sms", "booking.matched", content)
    assert res_unverified_sms.status == "suppressed"
    assert res_unverified_sms.reason == "phone_unverified"

    db.get.return_value = _make_user(uid, fcm_token=None)
    res_no_push = await service.send_notification(uid, "push", "booking.matched", content)
    assert res_no_push.status == "suppressed"
    assert res_no_push.reason == "no_device_token"

    # 3. Preferential enabled + verified -> queued
    db.get.return_value = _make_user(uid, fcm_token="fcm_device_token_123456")
    assert await service.resolve_contact(uid, "sms") == "2348012345678"
    res_queued = await service.send_notification(uid, "email", "booking.matched", content)
    assert res_queued.status == "queued"
    await bg()
    bg.tasks.clear()
    assert len(mock_email.sent_emails) == 1

    res_push_queued = await service.send_notification(uid, "push", "booking.offer", content)
    assert res_push_queued.status == "queued"
    await bg()
    bg.tasks.clear()
    assert len(mock_push.sent_pushes) == 1

    # 4. Transactional / system_critical bypass readiness and send inline
    db.get.return_value = _make_user(
        uid,
        is_active=False,
        email="u@example.com",
        email_verified_at=None,
        phone_verified_at=None,
    )
    res_tx = await service.send_notification(uid, "email", "password.reset", content)
    assert res_tx.status == "sent"
    assert len(mock_email.sent_emails) == 2

    res_sys = await service.send_notification(uid, "sms", "account.deleted", content)
    assert res_sys.status == "sent"
    assert len(mock_sms.sent_messages) == 1

    # 5. Savepoint failure & no-db during send_notification: distinct reasons
    res_no_db_pref = await NotificationService().send_notification(
        uid, "email", "booking.matched", content
    )
    assert res_no_db_pref.status == "suppressed"
    assert res_no_db_pref.reason == "no_db_session"

    db.execute.side_effect = RuntimeError("DB timeout")
    res_db_fail_pref = await service.send_notification(uid, "email", "booking.matched", content)
    assert res_db_fail_pref.status == "suppressed"
    assert res_db_fail_pref.reason == "preference_lookup_failed"

    res_db_fail_tx = await service.send_notification(uid, "email", "password.reset", content)
    assert res_db_fail_tx.status == "sent"

    # 6. Default dispatcher forwards injected providers
    fallback_service = NotificationService(
        sms_provider=mock_sms,
        email_provider=mock_email,
        push_provider=mock_push,
        db=db,
    )
    db.execute.side_effect = None
    db.execute.return_value = _scalar_one_or_none_result(True)
    db.get.return_value = _make_user(uid)
    res_fallback = await fallback_service.send_notification(
        uid, "email", "booking.matched", content
    )
    assert res_fallback.status == "sent"


def test_notification_schemas_validation_bounds() -> None:
    from app.modules.notifications.schemas import (
        NotificationContent,
        PreferenceItem,
        UpdatePreferencesRequest,
    )
    from pydantic import ValidationError

    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ValidationError):
            NotificationContent(body=blank)

    too_many = [
        PreferenceItem(channel="email", event_type="booking.matched", enabled=True)
        for _ in range(101)
    ]
    with pytest.raises(ValidationError):
        UpdatePreferencesRequest(preferences=too_many)

    exact_max = UpdatePreferencesRequest(preferences=too_many[:100])
    assert len(exact_max.preferences) == 100
