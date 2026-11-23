"""Booking service — lifecycle, pricing, dual-mode matching, and state machine."""

import uuid
from datetime import UTC, datetime
from typing import cast

import structlog
from app.core.exceptions import (
    BookingAlreadyAcceptedError,
    BookingNotFoundError,
    BookingOfferNotFoundError,
    BookingStateError,
    CategoryNotFoundError,
    ForbiddenError,
    HelperBusyError,
    HelperNotFoundError,
    InactiveUserError,
    NoAvailableHelpersError,
    UserNotFoundError,
)
from app.core.geo import bounding_box, haversine_km
from app.engines.matching import HelperCandidate, JobRequest, rank_helpers, score_helper
from app.engines.pricing import PricingRequest, recommend_price
from app.modules.bookings.model import Booking, BookingOffer
from app.modules.bookings.schemas import (
    BookingOfferSummary,
    BookingResponse,
    BookingStatus,
    BookingUrgency,
    CreateBookingRequest,
    CreateBookingResponse,
    MatchBookingResponse,
    MatchCandidateSchema,
    MatchingMode,
    OfferStatus,
    PaginatedBookingsResponse,
    PriceRangeSchema,
)
from app.modules.categories.model import ServiceCategory
from app.modules.helpers.model import HelperCategory, HelperProfile
from app.modules.notifications.push import PushProvider
from app.modules.users.model import User
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = structlog.get_logger(__name__)

AUTO_MATCH_TOP_N = 3
MANUAL_MATCH_TOP_N = 10
MAX_MATCH_SEARCH_RADIUS_KM = 50.0
DEFAULT_AVG_RESPONSE_MINUTES = 15.0


class BookingService:
    """Business logic for booking creation, matching, and state transitions."""

    def __init__(self, db: AsyncSession, push_provider: PushProvider) -> None:
        self._db = db
        self._push = push_provider

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def _get_active_user(
        self,
        user_id: uuid.UUID,
        *,
        required_role: str | None = None,
    ) -> User:
        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(context={"user_id": str(user_id)})
        if not user.is_active:
            raise InactiveUserError(context={"user_id": str(user_id)})
        if required_role is not None and user.role != required_role:
            raise ForbiddenError(
                detail=f"Only {required_role} accounts can perform this action.",
                context={"user_id": str(user_id), "role": user.role},
            )
        return user

    async def _get_booking(
        self,
        booking_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Booking:
        stmt = (
            select(Booking)
            .options(selectinload(Booking.category), selectinload(Booking.offers))
            .where(Booking.id == booking_id)
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._db.execute(stmt)
        booking = result.scalar_one_or_none()
        if booking is None:
            raise BookingNotFoundError(context={"booking_id": str(booking_id)})
        return booking

    async def _get_busy_helper_ids(self, helper_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if not helper_ids:
            return set()
        result = await self._db.execute(
            select(Booking.helper_id).where(
                Booking.helper_id.in_(helper_ids),
                Booking.status.in_(Booking.ACTIVE_HELPER_STATUSES),
            )
        )
        return {hid for hid in result.scalars().all() if hid is not None}

    @staticmethod
    def _to_offer_summary(offer: BookingOffer) -> BookingOfferSummary:
        return BookingOfferSummary(
            id=offer.id,
            helper_id=offer.helper_id,
            match_score=round(offer.match_score, 4),
            rank=offer.rank,
            status=cast(OfferStatus, offer.status),
            responded_at=offer.responded_at,
        )

    @classmethod
    def _to_booking_response(cls, booking: Booking) -> BookingResponse:
        offers = [
            cls._to_offer_summary(o)
            for o in sorted(booking.offers, key=lambda item: (item.rank, str(item.id)))
        ]
        category_name = booking.category.name
        category_slug = booking.category.slug
        return BookingResponse(
            id=booking.id,
            customer_id=booking.customer_id,
            helper_id=booking.helper_id,
            category_id=booking.category_id,
            category_name=category_name,
            category_slug=category_slug,
            status=cast(BookingStatus, booking.status),
            urgency=cast(BookingUrgency, booking.urgency),
            duration_hours=booking.duration_hours,
            matching_mode=cast(MatchingMode, booking.matching_mode),
            address=booking.address,
            latitude=booking.latitude,
            longitude=booking.longitude,
            notes=booking.notes,
            min_price=booking.min_price,
            recommended_price=booking.recommended_price,
            max_price=booking.max_price,
            agreed_price=booking.agreed_price,
            cancellation_reason=booking.cancellation_reason,
            cancelled_by=booking.cancelled_by,
            offers=offers,
            created_at=booking.created_at,
            updated_at=booking.updated_at,
        )

    async def _notify_helper_offer(self, booking: Booking, helper_id: uuid.UUID) -> None:
        category_label = booking.category.name
        try:
            await self._push.send_push(
                token=f"helper:{helper_id}",
                title="New UrgentHelp Job Offer",
                body=f"New {booking.urgency} {category_label} request near {booking.address}.",
                data={
                    "event": "booking.offer",
                    "booking_id": str(booking.id),
                    "urgency": booking.urgency,
                },
            )
        except Exception as exc:
            logger.warning(
                "booking_offer_push_failed",
                booking_id=str(booking.id),
                helper_id=str(helper_id),
                error=str(exc),
            )

    @staticmethod
    def _assert_transition(booking: Booking, target_status: str) -> None:
        if not booking.can_transition_to(target_status):
            raise BookingStateError(
                detail=f"Cannot transition booking from '{booking.status}' to '{target_status}'.",
                context={
                    "booking_id": str(booking.id),
                    "current": booking.status,
                    "attempted": target_status,
                },
            )

    # ─── Create ───────────────────────────────────────────────────────────────

    async def create_booking(
        self,
        customer_id: uuid.UUID,
        payload: CreateBookingRequest,
    ) -> CreateBookingResponse:
        """Create a booking in 'requested' status and return recommended pricing."""
        await self._get_active_user(customer_id, required_role="customer")

        cat_result = await self._db.execute(
            select(ServiceCategory).where(
                ServiceCategory.id == payload.category_id,
                ServiceCategory.is_active.is_(True),
            )
        )
        category = cat_result.scalar_one_or_none()
        if category is None:
            raise CategoryNotFoundError(context={"category_id": str(payload.category_id)})

        price = recommend_price(
            PricingRequest(
                category=category.slug,
                distance_km=0.0,
                urgency=payload.urgency,
                requested_at=datetime.now(UTC),
                duration_hours=payload.duration_hours,
                base_price_override=category.base_price_ngn,
            )
        )

        booking = Booking(
            customer_id=customer_id,
            category_id=category.id,
            status="requested",
            urgency=payload.urgency,
            duration_hours=payload.duration_hours,
            matching_mode=payload.matching_mode,
            address=payload.address,
            latitude=payload.latitude,
            longitude=payload.longitude,
            notes=payload.notes,
            min_price=price.minimum,
            recommended_price=price.recommended,
            max_price=price.maximum,
        )
        booking.category = category
        booking.offers = []

        self._db.add(booking)
        await self._db.commit()
        await self._db.refresh(booking)

        logger.info(
            "booking_created",
            booking_id=str(booking.id),
            customer_id=str(customer_id),
            category_slug=category.slug,
            matching_mode=booking.matching_mode,
            recommended_price=price.recommended,
        )

        base = self._to_booking_response(booking)
        return CreateBookingResponse(
            **base.model_dump(),
            price_range=PriceRangeSchema(
                minimum=price.minimum,
                recommended=price.recommended,
                maximum=price.maximum,
                currency=price.currency,
                confidence=price.confidence,
            ),
        )

    # ─── Match & Manual Selection ─────────────────────────────────────────────

    async def match_helpers(
        self,
        booking_id: uuid.UUID,
        customer_id: uuid.UUID,
        *,
        include_busy: bool = False,
    ) -> MatchBookingResponse:
        """Rank nearby helpers and dispatch offers (auto) or return candidates (manual)."""
        await self._get_active_user(customer_id, required_role="customer")
        booking = await self._get_booking(booking_id)

        if booking.customer_id != customer_id:
            raise ForbiddenError(
                detail="Only the booking owner can match helpers.",
                context={"booking_id": str(booking_id), "user_id": str(customer_id)},
            )
        if booking.status != "requested":
            raise BookingStateError(
                detail=(
                    f"Matching is only allowed in 'requested' status (current: '{booking.status}')."
                ),
                context={"booking_id": str(booking_id), "current": booking.status},
            )

        min_lat, max_lat, min_lon, max_lon = bounding_box(
            booking.latitude, booking.longitude, MAX_MATCH_SEARCH_RADIUS_KM
        )
        stmt = (
            select(HelperProfile, User)
            .join(User, HelperProfile.user_id == User.id)
            .join(HelperCategory, HelperCategory.helper_profile_id == HelperProfile.id)
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
                HelperCategory.category_id == booking.category_id,
            )
        )
        rows = (await self._db.execute(stmt)).all()

        declined_ids = {o.helper_id for o in booking.offers if o.status == "declined"}
        candidate_user_ids = [user.id for _, user in rows]
        busy_ids = await self._get_busy_helper_ids(candidate_user_ids)

        category_slug = booking.category.slug
        job = JobRequest(
            latitude=booking.latitude,
            longitude=booking.longitude,
            category=category_slug,
        )

        available_engine_inputs: list[HelperCandidate] = []
        row_by_helper_id: dict[str, tuple[HelperProfile, User, float]] = {}
        busy_candidates: list[MatchCandidateSchema] = []

        for profile, user in rows:
            if user.latitude is None or user.longitude is None:
                continue
            if user.id in declined_ids:
                continue
            dist = haversine_km(booking.latitude, booking.longitude, user.latitude, user.longitude)
            if dist > profile.service_radius_km:
                continue

            if user.id in busy_ids:
                if include_busy and booking.matching_mode == "manual":
                    busy_candidates.append(
                        MatchCandidateSchema(
                            helper_id=user.id,
                            full_name=user.full_name,
                            profile_photo_url=user.profile_photo_url,
                            is_id_verified=user.is_id_verified,
                            avg_rating=profile.avg_rating,
                            trust_score=user.trust_score,
                            completed_jobs_count=profile.completed_jobs_count,
                            nearest_landmark=profile.nearest_landmark,
                            distance_km=round(dist, 2),
                            match_score=0.0,
                            is_busy=True,
                        )
                    )
                continue

            skills = [cat.slug for cat in profile.categories] or [category_slug]
            available_engine_inputs.append(
                HelperCandidate(
                    helper_id=str(user.id),
                    latitude=user.latitude,
                    longitude=user.longitude,
                    avg_rating=profile.avg_rating,
                    trust_score=user.trust_score,
                    skills=skills,
                    avg_response_minutes=DEFAULT_AVG_RESPONSE_MINUTES,
                    service_radius_km=profile.service_radius_km,
                )
            )
            row_by_helper_id[str(user.id)] = (profile, user, dist)

        top_n = AUTO_MATCH_TOP_N if booking.matching_mode == "auto" else MANUAL_MATCH_TOP_N
        ranked = rank_helpers(available_engine_inputs, job, top_n=top_n)

        if not ranked:
            logger.warning(
                "no_helpers_available",
                booking_id=str(booking.id),
                category_slug=category_slug,
                busy_excluded=len(busy_ids),
            )
            raise NoAvailableHelpersError(
                context={"booking_id": str(booking.id), "category": category_slug}
            )

        candidates: list[MatchCandidateSchema] = []
        for item in ranked:
            profile, user, dist = row_by_helper_id[item.helper_id]
            candidates.append(
                MatchCandidateSchema(
                    helper_id=user.id,
                    full_name=user.full_name,
                    profile_photo_url=user.profile_photo_url,
                    is_id_verified=user.is_id_verified,
                    avg_rating=profile.avg_rating,
                    trust_score=user.trust_score,
                    completed_jobs_count=profile.completed_jobs_count,
                    nearest_landmark=profile.nearest_landmark,
                    distance_km=round(dist, 2),
                    match_score=round(item.score, 4),
                    is_busy=False,
                )
            )

        if booking.matching_mode == "manual":
            busy_candidates.sort(key=lambda c: c.distance_km)
            candidates.extend(busy_candidates)
            return MatchBookingResponse(
                booking_id=booking.id,
                status=cast(BookingStatus, booking.status),
                matching_mode="manual",
                notified_count=0,
                candidates=candidates,
                offers=[self._to_offer_summary(o) for o in booking.offers],
            )

        # Auto mode: persist top-3 offers, transition requested -> matched, notify top-3
        self._assert_transition(booking, "matched")
        existing_offers_by_helper = {o.helper_id: o for o in booking.offers}
        created_or_updated: list[BookingOffer] = []

        for idx, ranked_item in enumerate(ranked, start=1):
            helper_uuid = uuid.UUID(ranked_item.helper_id)
            existing = existing_offers_by_helper.get(helper_uuid)
            if existing is not None:
                existing.match_score = ranked_item.score
                existing.rank = idx
                existing.status = "pending"
                existing.responded_at = None
                created_or_updated.append(existing)
            else:
                offer = BookingOffer(
                    booking_id=booking.id,
                    helper_id=helper_uuid,
                    match_score=ranked_item.score,
                    rank=idx,
                    status="pending",
                )
                self._db.add(offer)
                booking.offers.append(offer)
                created_or_updated.append(offer)

        booking.status = "matched"
        await self._db.commit()
        await self._db.refresh(booking)

        for offer in created_or_updated:
            await self._notify_helper_offer(booking, offer.helper_id)

        logger.info(
            "booking_matched",
            booking_id=str(booking.id),
            matching_mode="auto",
            notified_count=len(created_or_updated),
            top_score=round(ranked[0].score, 3),
        )

        return MatchBookingResponse(
            booking_id=booking.id,
            status=cast(BookingStatus, booking.status),
            matching_mode="auto",
            notified_count=len(created_or_updated),
            candidates=candidates,
            offers=[self._to_offer_summary(o) for o in created_or_updated],
        )

    async def select_helper(
        self,
        booking_id: uuid.UUID,
        customer_id: uuid.UUID,
        helper_id: uuid.UUID,
    ) -> BookingResponse:
        """Manually select a single available helper and dispatch a targeted offer."""
        await self._get_active_user(customer_id, required_role="customer")
        booking = await self._get_booking(booking_id)

        if booking.customer_id != customer_id:
            raise ForbiddenError(
                detail="Only the booking owner can select a helper.",
                context={"booking_id": str(booking_id), "user_id": str(customer_id)},
            )
        self._assert_transition(booking, "matched")

        stmt = (
            select(HelperProfile, User)
            .join(User, HelperProfile.user_id == User.id)
            .join(HelperCategory, HelperCategory.helper_profile_id == HelperProfile.id)
            .options(selectinload(HelperProfile.categories))
            .where(
                User.id == helper_id,
                User.role == "helper",
                User.is_active.is_(True),
                HelperProfile.is_available.is_(True),
                HelperCategory.category_id == booking.category_id,
            )
        )
        row = (await self._db.execute(stmt)).first()
        if row is None:
            raise HelperNotFoundError(
                detail="Selected helper is unavailable or does not offer this service category.",
                context={"helper_id": str(helper_id), "category_id": str(booking.category_id)},
            )

        profile, helper_user = row
        if helper_user.latitude is None or helper_user.longitude is None:
            raise HelperNotFoundError(
                detail="Selected helper has no location configured.",
                context={"helper_id": str(helper_id)},
            )

        busy_ids = await self._get_busy_helper_ids([helper_id])
        if helper_id in busy_ids:
            raise HelperBusyError(context={"helper_id": str(helper_id)})

        dist = haversine_km(
            booking.latitude, booking.longitude, helper_user.latitude, helper_user.longitude
        )
        if dist > profile.service_radius_km:
            raise NoAvailableHelpersError(
                detail="Selected helper is outside their service radius for this booking.",
                context={"helper_id": str(helper_id), "distance_km": round(dist, 2)},
            )

        category_slug = booking.category.slug
        skills = [cat.slug for cat in profile.categories] or [category_slug]
        match_score = score_helper(
            HelperCandidate(
                helper_id=str(helper_user.id),
                latitude=helper_user.latitude,
                longitude=helper_user.longitude,
                avg_rating=profile.avg_rating,
                trust_score=helper_user.trust_score,
                skills=skills,
                avg_response_minutes=DEFAULT_AVG_RESPONSE_MINUTES,
                service_radius_km=profile.service_radius_km,
            ),
            JobRequest(
                latitude=booking.latitude,
                longitude=booking.longitude,
                category=category_slug,
            ),
        )

        existing = next((o for o in booking.offers if o.helper_id == helper_id), None)
        if existing is not None:
            existing.match_score = match_score
            existing.rank = 1
            existing.status = "pending"
            existing.responded_at = None
        else:
            offer = BookingOffer(
                booking_id=booking.id,
                helper_id=helper_id,
                match_score=match_score,
                rank=1,
                status="pending",
            )
            self._db.add(offer)
            booking.offers.append(offer)

        booking.status = "matched"
        await self._db.commit()
        await self._db.refresh(booking)

        await self._notify_helper_offer(booking, helper_id)

        logger.info(
            "booking_helper_selected",
            booking_id=str(booking.id),
            helper_id=str(helper_id),
            match_score=round(match_score, 3),
        )
        return self._to_booking_response(booking)

    # ─── State Transitions ────────────────────────────────────────────────────

    async def accept_booking(
        self,
        booking_id: uuid.UUID,
        helper_id: uuid.UUID,
    ) -> BookingResponse:
        """Accept a pending offer (matched -> accepted), locking the booking row."""
        await self._get_active_user(helper_id, required_role="helper")
        booking = await self._get_booking(booking_id, for_update=True)

        if booking.status == "accepted":
            raise BookingAlreadyAcceptedError(context={"booking_id": str(booking_id)})
        self._assert_transition(booking, "accepted")

        offer = next(
            (o for o in booking.offers if o.helper_id == helper_id and o.status == "pending"),
            None,
        )
        if offer is None:
            raise BookingOfferNotFoundError(
                context={"booking_id": str(booking_id), "helper_id": str(helper_id)}
            )

        busy_ids = await self._get_busy_helper_ids([helper_id])
        if helper_id in busy_ids:
            raise HelperBusyError(context={"helper_id": str(helper_id)})

        now = datetime.now(UTC)
        booking.status = "accepted"
        booking.helper_id = helper_id
        if booking.agreed_price is None:
            booking.agreed_price = booking.recommended_price

        offer.status = "accepted"
        offer.responded_at = now
        for other in booking.offers:
            if other.helper_id != helper_id and other.status == "pending":
                other.status = "superseded"
                other.responded_at = now

        try:
            await self._db.commit()
        except IntegrityError as exc:
            await self._db.rollback()
            raise HelperBusyError(
                context={"helper_id": str(helper_id), "booking_id": str(booking_id)}
            ) from exc

        await self._db.refresh(booking)
        logger.info(
            "booking_accepted",
            booking_id=str(booking.id),
            helper_id=str(helper_id),
        )
        return self._to_booking_response(booking)

    async def decline_booking(
        self,
        booking_id: uuid.UUID,
        helper_id: uuid.UUID,
    ) -> BookingResponse:
        """Decline a pending offer; reverts matched -> requested when no pending offers remain."""
        await self._get_active_user(helper_id, required_role="helper")
        booking = await self._get_booking(booking_id, for_update=True)

        if booking.status != "matched":
            raise BookingStateError(
                detail=(
                    "Offers can only be declined while booking is 'matched' "
                    f"(current: '{booking.status}')."
                ),
                context={"booking_id": str(booking_id), "current": booking.status},
            )

        offer = next(
            (o for o in booking.offers if o.helper_id == helper_id and o.status == "pending"),
            None,
        )
        if offer is None:
            raise BookingOfferNotFoundError(
                context={"booking_id": str(booking_id), "helper_id": str(helper_id)}
            )

        offer.status = "declined"
        offer.responded_at = datetime.now(UTC)

        has_remaining_pending = any(o.status == "pending" for o in booking.offers)
        if not has_remaining_pending:
            self._assert_transition(booking, "requested")
            booking.status = "requested"

        await self._db.commit()
        await self._db.refresh(booking)

        logger.info(
            "booking_offer_declined",
            booking_id=str(booking.id),
            helper_id=str(helper_id),
            reverted_to_requested=not has_remaining_pending,
        )
        return self._to_booking_response(booking)

    async def mark_en_route(
        self,
        booking_id: uuid.UUID,
        helper_id: uuid.UUID,
    ) -> BookingResponse:
        """Transition an accepted booking to en_route."""
        await self._get_active_user(helper_id, required_role="helper")
        booking = await self._get_booking(booking_id, for_update=True)

        if booking.helper_id != helper_id:
            raise ForbiddenError(
                detail="Only the assigned helper can update this booking.",
                context={"booking_id": str(booking_id), "helper_id": str(helper_id)},
            )
        self._assert_transition(booking, "en_route")
        booking.status = "en_route"
        await self._db.commit()
        await self._db.refresh(booking)

        logger.info("booking_en_route", booking_id=str(booking.id), helper_id=str(helper_id))
        return self._to_booking_response(booking)

    async def start_booking(
        self,
        booking_id: uuid.UUID,
        helper_id: uuid.UUID,
    ) -> BookingResponse:
        """Transition an en_route booking to in_progress (helper arrived & started job)."""
        await self._get_active_user(helper_id, required_role="helper")
        booking = await self._get_booking(booking_id, for_update=True)

        if booking.helper_id != helper_id:
            raise ForbiddenError(
                detail="Only the assigned helper can start this booking.",
                context={"booking_id": str(booking_id), "helper_id": str(helper_id)},
            )
        self._assert_transition(booking, "in_progress")
        booking.status = "in_progress"
        await self._db.commit()
        await self._db.refresh(booking)

        logger.info("booking_started", booking_id=str(booking.id), helper_id=str(helper_id))
        return self._to_booking_response(booking)

    async def complete_booking(
        self,
        booking_id: uuid.UUID,
        helper_id: uuid.UUID,
    ) -> BookingResponse:
        """Transition an in_progress booking to completed and increment helper job count."""
        await self._get_active_user(helper_id, required_role="helper")
        booking = await self._get_booking(booking_id, for_update=True)

        if booking.helper_id != helper_id:
            raise ForbiddenError(
                detail="Only the assigned helper can complete this booking.",
                context={"booking_id": str(booking_id), "helper_id": str(helper_id)},
            )
        self._assert_transition(booking, "completed")
        booking.status = "completed"

        profile_result = await self._db.execute(
            select(HelperProfile).where(HelperProfile.user_id == helper_id)
        )
        profile = profile_result.scalar_one_or_none()
        if profile is not None:
            profile.completed_jobs_count += 1

        await self._db.commit()
        await self._db.refresh(booking)

        logger.info("booking_completed", booking_id=str(booking.id), helper_id=str(helper_id))
        return self._to_booking_response(booking)

    async def cancel_booking(
        self,
        booking_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> BookingResponse:
        """Cancel a booking from a cancellable state (customer or assigned helper)."""
        await self._get_active_user(actor_id)
        booking = await self._get_booking(booking_id, for_update=True)

        if actor_id not in {booking.customer_id, booking.helper_id}:
            raise ForbiddenError(
                detail="Only the customer or assigned helper can cancel this booking.",
                context={"booking_id": str(booking_id), "user_id": str(actor_id)},
            )
        self._assert_transition(booking, "cancelled")

        now = datetime.now(UTC)
        booking.status = "cancelled"
        booking.cancellation_reason = reason
        booking.cancelled_by = actor_id
        for offer in booking.offers:
            if offer.status == "pending":
                offer.status = "expired"
                offer.responded_at = now

        await self._db.commit()
        await self._db.refresh(booking)

        logger.info(
            "booking_cancelled",
            booking_id=str(booking.id),
            cancelled_by=str(actor_id),
        )
        return self._to_booking_response(booking)

    # ─── Read / List ──────────────────────────────────────────────────────────

    async def get_booking(
        self,
        booking_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> BookingResponse:
        """Fetch booking detail for customer, assigned/offered helper, or admin."""
        actor = await self._get_active_user(actor_id)
        booking = await self._get_booking(booking_id)

        offered_helper_ids = {o.helper_id for o in booking.offers}
        is_authorized = (
            actor.role == "admin"
            or actor_id == booking.customer_id
            or actor_id == booking.helper_id
            or actor_id in offered_helper_ids
        )
        if not is_authorized:
            raise ForbiddenError(
                detail="You do not have access to this booking.",
                context={"booking_id": str(booking_id), "user_id": str(actor_id)},
            )
        return self._to_booking_response(booking)

    async def list_customer_bookings(
        self,
        customer_id: uuid.UUID,
        *,
        status: BookingStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedBookingsResponse:
        """List a customer's bookings newest-first with pagination."""
        await self._get_active_user(customer_id, required_role="customer")

        base_filter = [Booking.customer_id == customer_id]
        if status is not None:
            base_filter.append(Booking.status == status)

        total_result = await self._db.execute(select(func.count(Booking.id)).where(*base_filter))
        total = int(total_result.scalar_one())

        offset = (page - 1) * page_size
        rows_result = await self._db.execute(
            select(Booking)
            .options(selectinload(Booking.category), selectinload(Booking.offers))
            .where(*base_filter)
            .order_by(Booking.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        bookings = list(rows_result.scalars().all())
        return PaginatedBookingsResponse(
            items=[self._to_booking_response(b) for b in bookings],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def list_helper_bookings(
        self,
        helper_id: uuid.UUID,
        *,
        status: BookingStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedBookingsResponse:
        """List a helper's job queue (assigned bookings + pending offers)."""
        await self._get_active_user(helper_id, required_role="helper")

        pending_offer_subq = (
            select(BookingOffer.booking_id)
            .where(
                BookingOffer.helper_id == helper_id,
                BookingOffer.status == "pending",
            )
            .scalar_subquery()
        )
        ownership_filter = or_(
            Booking.helper_id == helper_id,
            Booking.id.in_(pending_offer_subq),
        )
        filters = [ownership_filter]
        if status is not None:
            filters.append(Booking.status == status)

        total_result = await self._db.execute(select(func.count(Booking.id)).where(*filters))
        total = int(total_result.scalar_one())

        offset = (page - 1) * page_size
        rows_result = await self._db.execute(
            select(Booking)
            .options(selectinload(Booking.category), selectinload(Booking.offers))
            .where(*filters)
            .order_by(Booking.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        bookings = list(rows_result.scalars().all())
        return PaginatedBookingsResponse(
            items=[self._to_booking_response(b) for b in bookings],
            total=total,
            page=page,
            page_size=page_size,
        )
