"""HelperProfile and HelperCategory ORM models."""

import uuid
from datetime import datetime

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.categories.model import ServiceCategory
from app.modules.users.model import User
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

_JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")


class HelperCategory(Base):
    """Join table linking a helper profile to active service categories."""

    __tablename__ = "helper_categories"

    helper_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("helper_profiles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_categories.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class HelperProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Role-specific extension profile for users with role='helper'."""

    __tablename__ = "helper_profiles"
    __table_args__ = (
        CheckConstraint(
            "service_radius_km > 0 AND service_radius_km <= 50",
            name="ck_helper_profiles_service_radius_range",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_radius_km: Mapped[float] = mapped_column(
        Float, default=10.0, server_default=text("10.0"), nullable=False
    )
    is_available: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False, index=True
    )
    availability_schedule: Mapped[dict[str, object] | None] = mapped_column(
        _JSONType, nullable=True
    )
    certifications: Mapped[list[str]] = mapped_column(
        _JSONType, default=list, server_default=text("'[]'"), nullable=False
    )
    avg_rating: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0.0"), nullable=False
    )
    completed_jobs_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    categories: Mapped[list["ServiceCategory"]] = relationship(
        "ServiceCategory",
        secondary=HelperCategory.__table__,
        lazy="selectin",
    )
