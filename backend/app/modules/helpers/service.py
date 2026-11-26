"""Helper service — helper profile management and geospatial nearby search."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from app.core.exceptions import (
    CategoryNotFoundError,
    ForbiddenError,
    InactiveUserError,
    UserNotFoundError,
)
from app.core.geo import bounding_box, haversine_km
from app.modules.bookings.model import Booking
from app.modules.bookings.service import (
    DEFAULT_CONFLICT_BUFFER_MINUTES,
    booking_time_window,
    windows_overlap,
)
from app.modules.categories.model import ServiceCategory
from app.modules.helpers.model import HelperCategory, HelperProfile
from app.modules.helpers.schemas import (
    AddressType,
    DayOfWeek,
    DaySchedule,
    HelperCategorySummary,
    HelperProfileResponse,
    NearbyHelperResponse,
    UpdateHelperProfileRequest,
)
from app.modules.users.model import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = structlog.get_logger(__name__)


class HelperService:
    """Business logic for helper profiles and nearby helper discovery."""

    def __init__(
        self,
        db: AsyncSession,
        conflict_buffer_minutes: int = DEFAULT_CONFLICT_BUFFER_MINUTES,
    ) -> None:
        self._db = db
        self._conflict_buffer_minutes = conflict_buffer_minutes

    async def _get_active_helper_user(self, user_id: uuid.UUID) -> User:
        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(context={"user_id": str(user_id)})
        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user_id)})
        if user.role != "helper":
            raise ForbiddenError(
                detail="Only helper accounts can access helper profile endpoints.",
                context={"user_id": str(user_id), "role": user.role},
            )
        return user

    async def _get_or_create_profile(self, user: User) -> HelperProfile:
        result = await self._db.execute(
            select(HelperProfile)
            .options(selectinload(HelperProfile.categories))
            .where(HelperProfile.user_id == user.id)
        )
        profile = result.scalar_one_or_none()
        if profile is not None:
            return profile

        profile = HelperProfile(user_id=user.id)
        self._db.add(profile)
        await self._db.commit()
        await self._db.refresh(profile)
        return profile

    @staticmethod
    def _parse_schedule(
        raw: dict[str, object] | None,
    ) -> dict[DayOfWeek, DaySchedule] | None:
        if raw is None:
            return None
        parsed: dict[DayOfWeek, DaySchedule] = {}
        for day_key, window in raw.items():
            parsed[cast(DayOfWeek, day_key)] = DaySchedule.model_validate(window)
        return parsed

    @classmethod
    def _to_profile_response(cls, user: User, profile: HelperProfile) -> HelperProfileResponse:
        categories = [
            HelperCategorySummary.model_validate(cat)
            for cat in sorted(profile.categories, key=lambda c: c.name)
        ]
        return HelperProfileResponse(
            id=profile.id,
            user_id=user.id,
            full_name=user.full_name,
            profile_photo_url=user.profile_photo_url,
            is_id_verified=user.is_id_verified,
            trust_score=user.trust_score,
            address=user.address,
            address_type=cast(AddressType | None, user.address_type),
            latitude=user.latitude,
            longitude=user.longitude,
            nearest_landmark=profile.nearest_landmark,
            bio=profile.bio,
            service_radius_km=profile.service_radius_km,
            is_available=profile.is_available,
            availability_schedule=cls._parse_schedule(profile.availability_schedule),
            certifications=list(profile.certifications),
            avg_rating=profile.avg_rating,
            completed_jobs_count=profile.completed_jobs_count,
            categories=categories,
        )

    async def get_my_profile(self, user_id: uuid.UUID) -> HelperProfileResponse:
        """Fetch (or lazily initialize) the authenticated helper's profile."""
        user = await self._get_active_helper_user(user_id)
        profile = await self._get_or_create_profile(user)
        return self._to_profile_response(user, profile)

    async def update_helper_profile(
        self,
        user_id: uuid.UUID,
        payload: UpdateHelperProfileRequest,
    ) -> HelperProfileResponse:
        """Update helper skills (categories), radius, availability, bio, and certifications."""
        user = await self._get_active_helper_user(user_id)
        profile = await self._get_or_create_profile(user)
        fields = payload.model_fields_set

        if "bio" in fields:
            profile.bio = payload.bio
        if "nearest_landmark" in fields and payload.nearest_landmark is not None:
            profile.nearest_landmark = payload.nearest_landmark
        if "address_type" in fields and payload.address_type is not None:
            user.address_type = payload.address_type
        if "service_radius_km" in fields and payload.service_radius_km is not None:
            profile.service_radius_km = payload.service_radius_km
        if "is_available" in fields and payload.is_available is not None:
            profile.is_available = payload.is_available
        if "availability_schedule" in fields:
            if payload.availability_schedule is None:
                profile.availability_schedule = None
            else:
                profile.availability_schedule = {
                    day: window.model_dump()
                    for day, window in payload.availability_schedule.items()
                }
        if "certifications" in fields and payload.certifications is not None:
            profile.certifications = list(payload.certifications)

        if "category_ids" in fields and payload.category_ids is not None:
            deduped_ids = list(dict.fromkeys(payload.category_ids))
            if not deduped_ids:
                profile.categories = []
            else:
                cat_result = await self._db.execute(
                    select(ServiceCategory).where(
                        ServiceCategory.id.in_(deduped_ids),
                        ServiceCategory.is_active.is_(True),
                    )
                )
                found_categories = list(cat_result.scalars().all())
                found_ids = {cat.id for cat in found_categories}
                missing = [str(cid) for cid in deduped_ids if cid not in found_ids]
                if missing:
                    raise CategoryNotFoundError(
                        detail="One or more service categories do not exist or are inactive.",
                        context={"missing_category_ids": missing},
                    )
                profile.categories = found_categories

        await self._db.commit()
        await self._db.refresh(profile)

        logger.info("helper_profile_updated", user_id=str(user_id), profile_id=str(profile.id))
        return self._to_profile_response(user, profile)

    async def get_helpers_nearby(
        self,
        *,
        latitude: float,
        longitude: float,
        radius_km: float = 10.0,
        category_id: uuid.UUID | None = None,
        limit: int = 20,
    ) -> list[NearbyHelperResponse]:
        """Find active, available helpers within both the search radius and their service radius."""
        min_lat, max_lat, min_lon, max_lon = bounding_box(latitude, longitude, radius_km)

        stmt = (
            select(HelperProfile, User)
            .join(User, HelperProfile.user_id == User.id)
            .options(selectinload(HelperProfile.categories))
            .where(
                User.role == "helper",
                User.is_active.is_(True),
                User.latitude.is_not(None),
                User.longitude.is_not(None),
                User.latitude >= min_lat,
                User.latitude <= max_lat,
                User.longitude >= min_lon,
                User.longitude <= max_lon,
                HelperProfile.is_available.is_(True),
            )
        )
        if category_id is not None:
            stmt = stmt.join(
                HelperCategory,
                HelperCategory.helper_profile_id == HelperProfile.id,
            ).where(HelperCategory.category_id == category_id)

        rows = (await self._db.execute(stmt)).all()

        candidate_user_ids = [user.id for _, user in rows]
        busy_helper_ids: set[uuid.UUID] = set()
        if candidate_user_ids:
            busy_result = await self._db.execute(
                select(Booking).where(
                    Booking.helper_id.in_(candidate_user_ids),
                    Booking.status.in_(Booking.ACTIVE_HELPER_STATUSES),
                )
            )
            now = datetime.now(UTC)
            probe_end = now + timedelta(hours=1)
            for active_booking in busy_result.scalars().all():
                if active_booking.helper_id is None:
                    continue
                if active_booking.status in Booking.IN_FLIGHT_HELPER_STATUSES:
                    busy_helper_ids.add(active_booking.helper_id)
                    continue
                act_start, act_end = booking_time_window(
                    scheduled_at=active_booking.scheduled_at,
                    created_at=active_booking.created_at,
                    duration_hours=active_booking.duration_hours,
                    now=now,
                    status=active_booking.status,
                )
                if windows_overlap(
                    now,
                    probe_end,
                    act_start,
                    act_end,
                    buffer_minutes=self._conflict_buffer_minutes,
                ):
                    busy_helper_ids.add(active_booking.helper_id)

        nearby: list[NearbyHelperResponse] = []
        for profile, user in rows:
            if user.latitude is None or user.longitude is None:
                continue
            dist = haversine_km(latitude, longitude, user.latitude, user.longitude)
            effective_radius = min(radius_km, profile.service_radius_km)
            if dist <= effective_radius:
                categories = [
                    HelperCategorySummary.model_validate(cat)
                    for cat in sorted(profile.categories, key=lambda c: c.name)
                ]
                nearby.append(
                    NearbyHelperResponse(
                        helper_id=user.id,
                        profile_id=profile.id,
                        full_name=user.full_name,
                        profile_photo_url=user.profile_photo_url,
                        is_id_verified=user.is_id_verified,
                        trust_score=user.trust_score,
                        avg_rating=profile.avg_rating,
                        completed_jobs_count=profile.completed_jobs_count,
                        bio=profile.bio,
                        nearest_landmark=profile.nearest_landmark,
                        service_radius_km=profile.service_radius_km,
                        distance_km=round(dist, 2),
                        is_busy=user.id in busy_helper_ids,
                        categories=categories,
                    )
                )

        nearby.sort(key=lambda item: (item.is_busy, item.distance_km))
        return nearby[:limit]
