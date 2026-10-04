"""User ORM model."""

from datetime import datetime
from typing import TYPE_CHECKING

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy import Boolean, DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.modules.bookings.model import Booking


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Represents both customers and helpers — role field differentiates them."""

    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    # Email is NOT a login identifier. It is a verified recovery/notification channel.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    # Only a verified email may receive password reset links.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        Enum("customer", "helper", "admin", name="user_role_enum"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_id_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    profile_photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trust_score: Mapped[float] = mapped_column(default=50.0, nullable=False)

    bookings_as_customer: Mapped[list["Booking"]] = relationship(
        "Booking", foreign_keys="Booking.customer_id", back_populates="customer"
    )
