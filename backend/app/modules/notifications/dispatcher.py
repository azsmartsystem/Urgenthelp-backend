"""Pre-dispatch notification delivery protocols and M1 InlineDispatcher."""

import uuid
from dataclasses import dataclass
from typing import Protocol

import sentry_sdk
import structlog
from app.core.exceptions import (
    EmailDispatchError,
    ExternalServiceError,
    PushDispatchError,
    SMSDispatchError,
    UrgentHelpError,
)
from app.core.validators import CanonicalPhone
from app.modules.notifications.email import EmailProvider
from app.modules.notifications.push import PushProvider
from app.modules.notifications.registry import (
    NON_OPT_OUT_CLASSES,
    EventClass,
    validate_channel,
)
from app.modules.notifications.schemas import DeliveryResult, NotificationContent
from app.modules.notifications.sms import SMSProvider
from fastapi import BackgroundTasks

logger = structlog.get_logger(__name__)


def mask_email(email: str) -> str:
    """Mask email local part for PII-safe structured logging."""
    parts = email.split("@", 1)
    if len(parts) != 2:
        return "***"
    name, domain = parts
    masked_name = name[0] + "***" if len(name) > 1 else "***"
    return f"{masked_name}@{domain}"


def mask_recipient(channel: str, recipient: str) -> str:
    """Mask recipient identifier according to channel PII sensitivity."""
    if channel == "email":
        return mask_email(recipient)
    if channel == "sms":
        return f"...{recipient[-4:]}" if len(recipient) >= 4 else "***"
    if channel == "push":
        return f"...{recipient[-4:]}" if len(recipient) >= 8 else "***"
    return "***"


@dataclass(frozen=True, slots=True)
class DispatchPayload:
    """Immutable, DB-free notification payload handed off to a dispatcher."""

    user_id: uuid.UUID
    channel: str
    event_type: str
    event_class: EventClass
    recipient: str
    content: NotificationContent


class NotificationDispatcher(Protocol):
    """Port for dispatching resolved, DB-free notification payloads."""

    async def dispatch(self, payload: DispatchPayload) -> DeliveryResult:
        """Dispatch a notification payload and return its delivery status."""
        ...


class InlineDispatcher:
    """M1 dispatcher: inline await for critical events, BackgroundTasks for preferential."""

    def __init__(
        self,
        sms_provider: SMSProvider,
        email_provider: EmailProvider,
        push_provider: PushProvider,
        background_tasks: BackgroundTasks | None = None,
    ) -> None:
        self._sms_provider = sms_provider
        self._email_provider = email_provider
        self._push_provider = push_provider
        self._background_tasks = background_tasks

    async def dispatch(self, payload: DispatchPayload) -> DeliveryResult:
        """Dispatch payload inline or via FastAPI BackgroundTasks depending on event class."""
        validate_channel(payload.channel)
        if payload.event_class in NON_OPT_OUT_CLASSES:
            return await self._send_inline_strict(payload)

        if self._background_tasks is not None:
            self._background_tasks.add_task(self._run_isolated, payload)
            logger.info(
                "notification_queued",
                status="queued",
                user_id=str(payload.user_id),
                channel=payload.channel,
                event_type=payload.event_type,
                recipient=mask_recipient(payload.channel, payload.recipient),
            )
            return DeliveryResult(status="queued")

        succeeded = await self._run_isolated(payload)
        return DeliveryResult(status="sent" if succeeded else "failed")

    async def _send_inline_strict(self, payload: DispatchPayload) -> DeliveryResult:
        masked_recipient = mask_recipient(payload.channel, payload.recipient)
        try:
            await self._send_via_provider(payload)
        except UrgentHelpError as exc:
            logger.error(
                "notification_inline_failed",
                status="failed",
                user_id=str(payload.user_id),
                channel=payload.channel,
                event_type=payload.event_type,
                recipient=masked_recipient,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        except Exception as exc:
            logger.error(
                "notification_inline_failed",
                status="failed",
                user_id=str(payload.user_id),
                channel=payload.channel,
                event_type=payload.event_type,
                recipient=masked_recipient,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise self._wrap_provider_error(payload.channel, exc) from exc

        logger.info(
            "notification_dispatched",
            status="sent",
            user_id=str(payload.user_id),
            channel=payload.channel,
            event_type=payload.event_type,
            recipient=masked_recipient,
        )
        return DeliveryResult(status="sent")

    async def _run_isolated(self, payload: DispatchPayload) -> bool:
        """Execute preferential delivery inside a zero-throw boundary."""
        masked_recipient = mask_recipient(payload.channel, payload.recipient)
        try:
            await self._send_via_provider(payload)
        except Exception as exc:
            logger.error(
                "notification_dispatch_failed",
                status="failed",
                user_id=str(payload.user_id),
                channel=payload.channel,
                event_type=payload.event_type,
                recipient=masked_recipient,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if sentry_sdk.is_initialized():
                sentry_sdk.capture_exception(exc)
            return False

        logger.info(
            "notification_dispatched",
            status="sent",
            user_id=str(payload.user_id),
            channel=payload.channel,
            event_type=payload.event_type,
            recipient=masked_recipient,
        )
        return True

    async def _send_via_provider(self, payload: DispatchPayload) -> None:
        if payload.channel == "sms":
            await self._sms_provider.send_sms(
                to=CanonicalPhone(payload.recipient),
                message=payload.content.body,
            )
            return

        if payload.channel == "email":
            subject = payload.content.subject or payload.content.title or "UrgentHelp Notification"
            await self._email_provider.send_email(
                to=payload.recipient,
                subject=subject,
                body=payload.content.body,
                html_body=payload.content.html_body,
            )
            return

        title = payload.content.title or payload.content.subject or "UrgentHelp"
        await self._push_provider.send_push(
            token=payload.recipient,
            title=title,
            body=payload.content.body,
            data=payload.content.data,
        )

    @staticmethod
    def _wrap_provider_error(channel: str, exc: Exception) -> UrgentHelpError:
        context: dict[str, object] = {"channel": channel, "error": str(exc)}
        if channel == "sms":
            return SMSDispatchError(context=context)
        if channel == "email":
            return EmailDispatchError(context=context)
        if channel == "push":
            return PushDispatchError(context=context)
        return ExternalServiceError(context=context)
