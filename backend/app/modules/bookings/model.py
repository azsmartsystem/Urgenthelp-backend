"""Booking ORM model and state machine."""

import uuid
from typing import TYPE_CHECKING, ClassVar

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy import Enum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.modules.categories.model import ServiceCategory
    from app.modules.users.model import User


class Booking(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bookings"

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

    address: Mapped[str] = mapped_column(String(500), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    agreed_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    customer: Mapped["User"] = relationship(
        "User", foreign_keys=[customer_id], back_populates="bookings_as_customer"
    )
    category: Mapped["ServiceCategory"] = relationship("ServiceCategory")

    # Valid state transitions — enforced by BookingService, not the DB
    VALID_TRANSITIONS: ClassVar[dict[str, list[str]]] = {
        "requested": ["matched", "cancelled"],
        "matched": ["accepted", "cancelled"],
        "accepted": ["en_route", "cancelled"],
        "en_route": ["in_progress"],
        "in_progress": ["completed", "disputed"],
        "completed": [],
        "cancelled": [],
        "disputed": ["completed", "cancelled"],
    }

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.VALID_TRANSITIONS.get(self.status, [])
