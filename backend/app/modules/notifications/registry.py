"""Canonical notification event taxonomy, channels, and default preferences."""

from enum import StrEnum

from app.core.exceptions import (
    InvalidNotificationChannelError,
    InvalidNotificationEventError,
)


class EventClass(StrEnum):
    """Classification governing opt-out eligibility, dispatch mode, and degradation."""

    TRANSACTIONAL = "transactional"
    PREFERENTIAL = "preferential"
    SYSTEM_CRITICAL = "system_critical"


VALID_CHANNELS: tuple[str, ...] = ("push", "email", "sms")

NON_OPT_OUT_CLASSES: frozenset[EventClass] = frozenset(
    {EventClass.TRANSACTIONAL, EventClass.SYSTEM_CRITICAL}
)

EVENT_REGISTRY: dict[str, EventClass] = {
    "password.reset": EventClass.TRANSACTIONAL,
    "otp.sent": EventClass.TRANSACTIONAL,
    "payment.received": EventClass.TRANSACTIONAL,
    "security.alert": EventClass.TRANSACTIONAL,
    "booking.matched": EventClass.PREFERENTIAL,
    "booking.offer": EventClass.PREFERENTIAL,
    "review.request": EventClass.PREFERENTIAL,
    "promo.marketing": EventClass.PREFERENTIAL,
    "account.deleted": EventClass.SYSTEM_CRITICAL,
    "terms.updated": EventClass.SYSTEM_CRITICAL,
}

TRANSACTIONAL_EVENTS: frozenset[str] = frozenset(
    event_type
    for event_type, event_class in EVENT_REGISTRY.items()
    if event_class in NON_OPT_OUT_CLASSES
)


def _compute_default_enabled(channel: str, event_class: EventClass) -> bool:
    if event_class == EventClass.PREFERENTIAL and channel == "sms":
        return False
    return True


DEFAULT_PREFERENCES: dict[tuple[str, str], bool] = {
    (channel, event_type): _compute_default_enabled(channel, event_class)
    for channel in VALID_CHANNELS
    for event_type, event_class in EVENT_REGISTRY.items()
}


def validate_channel(channel: str) -> str:
    """Validate channel against VALID_CHANNELS or raise InvalidNotificationChannelError."""
    if channel not in VALID_CHANNELS:
        raise InvalidNotificationChannelError(
            detail=f"Unsupported notification channel: '{channel}'.",
            context={"channel": channel},
        )
    return channel


def get_event_class(event_type: str) -> EventClass:
    """Resolve event_type to its EventClass or raise InvalidNotificationEventError."""
    event_class = EVENT_REGISTRY.get(event_type)
    if event_class is None:
        raise InvalidNotificationEventError(
            detail=f"Unknown notification event type: '{event_type}'.",
            context={"event_type": event_type},
        )
    return event_class
