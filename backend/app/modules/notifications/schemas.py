"""Pydantic DTOs for notification preferences and pre-dispatch delivery."""

from typing import Literal

from app.schemas.base import AppBaseModel
from pydantic import ConfigDict, Field, field_validator

DeliveryStatus = Literal["sent", "queued", "suppressed", "failed"]


class PreferenceItem(AppBaseModel):
    """Single (channel, event_type, enabled) notification preference entry."""

    channel: str
    event_type: str
    enabled: bool


class PreferencesResponse(AppBaseModel):
    """Materialized preference list covering VALID_CHANNELS x EVENT_REGISTRY."""

    preferences: list[PreferenceItem]


class UpdatePreferencesRequest(AppBaseModel):
    """Batch MERGE update request for user notification preferences."""

    preferences: list[PreferenceItem] = Field(max_length=100)


class NotificationContent(AppBaseModel):
    """Immutable typed content payload passed to NotificationService.send_notification."""

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
        frozen=True,
    )

    body: str = Field(min_length=1)
    subject: str | None = None
    title: str | None = None
    html_body: str | None = None
    data: dict[str, str] | None = None

    @field_validator("body")
    @classmethod
    def reject_blank_body(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Notification body must not be blank.")
        return v


class DeliveryResult(AppBaseModel):
    """Result of a pre-dispatch notification attempt."""

    status: DeliveryStatus
    reason: str | None = None
