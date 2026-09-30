"""ServiceCategory ORM model."""

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from sqlalchemy import Boolean, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column


class ServiceCategory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Admin-configurable service category."""

    __tablename__ = "service_categories"

    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_price_ngn: Mapped[float] = mapped_column(Float, nullable=False, default=5000.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
