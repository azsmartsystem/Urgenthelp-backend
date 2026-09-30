"""User ORM model."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.modules.bookings.model import Booking


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Represents both customers and helpers — role field differentiates them."""

    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
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
