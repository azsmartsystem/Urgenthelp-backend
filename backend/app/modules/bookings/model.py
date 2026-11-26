"""Booking and BookingOffer ORM models and booking state machine."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Runtime import (not TYPE_CHECKING): relationship() targets are resolved from
# SQLAlchemy's class registry at mapper-config time, so a TYPE_CHECKING-only
# import makes the mapper fail with "failed to locate a name".
from app.modules.categories.model import ServiceCategory
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.modules.users.model import User


class Booking(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(
            "duration_hours > 0 AND duration_hours <= 24",
            name="ck_bookings_duration_hours_range",
        ),
        Index(
            "uq_bookings_active_helper",
            "helper_id",
            unique=True,
            postgresql_where=text(
                "helper_id IS NOT NULL AND status IN ('en_route', 'in_progress')"
            ),
        ),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    helper_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_categories.id"), nullable=False
    )

    status: Mapped[str] = mapped_column(
        Enum(
            "requested",
            "matched",
            "accepted",
            "en_route",
            "in_progress",
            "completed",
            "cancelled",
            "disputed",
            name="booking_status_enum",
        ),
        nullable=False,
        default="requested",
        index=True,
    )
    urgency: Mapped[str] = mapped_column(
        Enum("standard", "urgent", name="booking_urgency_enum"),
        nullable=False,
        default="standard",
        server_default=text("'standard'"),
    )
    duration_hours: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default=text("1.0"),
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    matching_mode: Mapped[str] = mapped_column(
        Enum("auto", "manual", name="booking_matching_mode_enum"),
        nullable=False,
        default="auto",
        server_default=text("'auto'"),
    )

    address: Mapped[str] = mapped_column(String(500), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    recommended_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    agreed_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    customer: Mapped["User"] = relationship(
        "User", foreign_keys=[customer_id], back_populates="bookings_as_customer"
    )
    helper: Mapped["User | None"] = relationship("User", foreign_keys=[helper_id])
    category: Mapped[ServiceCategory] = relationship(ServiceCategory, lazy="selectin")
    offers: Mapped[list["BookingOffer"]] = relationship(
        "BookingOffer",
        back_populates="booking",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Statuses during which a helper is actively committed for overlapping slots.
    ACTIVE_HELPER_STATUSES: ClassVar[tuple[str, ...]] = ("accepted", "en_route", "in_progress")
    # Statuses during which a helper is physically executing or travelling to a single job.
    IN_FLIGHT_HELPER_STATUSES: ClassVar[tuple[str, ...]] = ("en_route", "in_progress")

    # Valid state transitions — enforced by BookingService, not the DB.
    VALID_TRANSITIONS: ClassVar[dict[str, list[str]]] = {
        "requested": ["matched", "cancelled"],
        "matched": ["accepted", "requested", "cancelled"],
        "accepted": ["en_route", "requested", "cancelled"],
        "en_route": ["in_progress"],
        "in_progress": ["completed", "disputed"],
        "completed": [],
        "cancelled": [],
        "disputed": ["completed", "cancelled"],
    }

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, [])


class BookingOffer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Offer sent to a matched helper for a booking (auto top-3 or manual single)."""

    __tablename__ = "booking_offers"
    __table_args__ = (
        UniqueConstraint("booking_id", "helper_id", name="uq_booking_offers_booking_helper"),
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    helper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "pending",
            "accepted",
            "declined",
            "superseded",
            "expired",
            name="booking_offer_status_enum",
        ),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
        index=True,
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    booking: Mapped[Booking] = relationship("Booking", back_populates="offers")
    helper: Mapped["User"] = relationship("User", foreign_keys=[helper_id])
