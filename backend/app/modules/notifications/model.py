"""Notification preference ORM model."""

import uuid

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class NotificationPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-user opt-in/out for one (channel, event_type) pair.

    Transactional events (password reset, security alerts, payment receipts) are
    never stored here: they are always delivered and cannot be opted out of.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "channel", "event_type", name="uq_notification_pref"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(
        Enum("push", "email", "sms", name="notification_channel_enum"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
