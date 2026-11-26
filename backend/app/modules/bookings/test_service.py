"""Unit & router tests for BookingService, state machine, dual-mode matching, and endpoints."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import (
    BookingAlreadyAcceptedError,
    BookingNotFoundError,
    BookingOfferNotFoundError,
    BookingStateError,
    CategoryNotFoundError,
    ForbiddenError,
    HelperBusyError,
    HelperNotFoundError,
    NoAvailableHelpersError,
)
from app.core.security import TokenPayload, get_current_user
from app.main import app
from app.modules.bookings.model import Booking, BookingOffer
from app.modules.bookings.router import _get_service as _get_booking_service
from app.modules.bookings.schemas import (
    CreateBookingRequest,
    PaginatedBookingsResponse,
    RescheduleBookingRequest,
)
from app.modules.bookings.service import (
    BookingService,
    booking_time_window,
    windows_overlap,
)
from app.modules.categories.model import ServiceCategory
from app.modules.helpers.model import HelperProfile
from app.modules.helpers.router import _get_booking_service as _get_helper_booking_service
from app.modules.notifications.push import MockPushProvider
from app.modules.users.model import User
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError


def _make_user(
    *,
    user_id: uuid.UUID | None = None,
    role: str = "customer",
    is_active: bool = True,
    latitude: float | None = 6.5244,
    longitude: float | None = 3.3792,
    full_name: str = "Adaeze Obi",
) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        phone="2348012345678",
        full_name=full_name,
        hashed_password="hash",
        role=role,
        is_active=is_active,
        is_id_verified=True,
        is_phone_verified=True,
        trust_score=85.0,
        address="Lekki Phase 1, Lagos",
        latitude=latitude,
        longitude=longitude,
        profile_photo_url=None,
    )


def _make_category(
    *,
    name: str = "Plumbing",
    slug: str = "plumbing",
    base_price_ngn: float = 8000.0,
) -> ServiceCategory:
    return ServiceCategory(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        description="Pipe repairs",
        base_price_ngn=base_price_ngn,
        is_active=True,
    )


def _make_profile(
    user: User,
    *,
    service_radius_km: float = 15.0,
    is_available: bool = True,
    categories: list[ServiceCategory] | None = None,
    avg_rating: float = 4.7,
    completed_jobs_count: int = 5,
) -> HelperProfile:
    profile = HelperProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        bio="Reliable Lagos technician",
        nearest_landmark="Lekki Toll Gate",
        service_radius_km=service_radius_km,
        is_available=is_available,
        certifications=["NABTEB"],
        avg_rating=avg_rating,
        completed_jobs_count=completed_jobs_count,
    )
    profile.categories = categories or []
    return profile


def _make_booking(
    customer: User,
    category: ServiceCategory,
    *,
    status: str = "requested",
    matching_mode: str = "auto",
    helper_id: uuid.UUID | None = None,
    offers: list[BookingOffer] | None = None,
    scheduled_at: datetime | None = None,
    duration_hours: float = 1.0,
    created_at: datetime | None = None,
) -> Booking:
    now = created_at or datetime.now(UTC)
    booking = Booking(
        id=uuid.uuid4(),
        customer_id=customer.id,
        helper_id=helper_id,
        category_id=category.id,
        status=status,
        urgency="standard",
        duration_hours=duration_hours,
        scheduled_at=scheduled_at,
        matching_mode=matching_mode,
        address="14 Admiralty Way, Lekki Phase 1, Lagos",
        latitude=6.4474,
        longitude=3.4723,
        notes="Kitchen sink leak",
        min_price=6800.0,
        recommended_price=8000.0,
        max_price=9600.0,
        agreed_price=None,
        created_at=now,
        updated_at=now,
    )
    booking.category = category
    booking.offers = offers if offers is not None else []
    return booking


def _make_offer(
    booking: Booking,
    helper_id: uuid.UUID,
    *,
    rank: int = 1,
    match_score: float = 0.85,
    status: str = "pending",
) -> BookingOffer:
    return BookingOffer(
        id=uuid.uuid4(),
        booking_id=booking.id,
        helper_id=helper_id,
        match_score=match_score,
        rank=rank,
        status=status,
        responded_at=None,
    )


def _scalar_result(value: object) -> MagicMock:
    res = MagicMock()
    res.scalar_one_or_none.return_value = value
    return res


def _scalar_one_result(value: object) -> MagicMock:
    res = MagicMock()
    res.scalar_one.return_value = value
    return res


def _scalars_all_result(values: list[object]) -> MagicMock:
    res = MagicMock()
    res.scalars.return_value.all.return_value = values
    return res


def _rows_all_result(rows: list[tuple[object, ...]]) -> MagicMock:
    res = MagicMock()
    res.all.return_value = rows
    return res


def _first_row_result(row: tuple[object, ...] | None) -> MagicMock:
    res = MagicMock()
    res.first.return_value = row
    return res


# ─── Schema & State Machine Tests ────────────────────────────────────────────


def test_create_booking_request_rejects_null_island() -> None:
    with pytest.raises(ValidationError, match=r"0\.0, 0\.0"):
        CreateBookingRequest(
            category_id=uuid.uuid4(),
            address="14 Admiralty Way, Lekki",
            latitude=0.0,
            longitude=0.0,
        )


def test_booking_state_machine_transitions() -> None:
    customer = _make_user(role="customer")
    cat = _make_category()
    b = _make_booking(customer, cat, status="requested")

    assert b.can_transition_to("matched")
    assert b.can_transition_to("cancelled")
    assert not b.can_transition_to("completed")

    b.status = "matched"
    assert b.can_transition_to("accepted")
    assert b.can_transition_to("requested")
    assert b.can_transition_to("cancelled")

    b.status = "accepted"
    assert b.can_transition_to("en_route")
    assert b.can_transition_to("requested")
    assert b.can_transition_to("cancelled")
    assert not b.can_transition_to("in_progress")

    b.status = "en_route"
    assert b.can_transition_to("in_progress")
    assert not b.can_transition_to("cancelled")

    b.status = "in_progress"
    assert b.can_transition_to("completed")
    assert b.can_transition_to("disputed")
    assert not b.can_transition_to("cancelled")


# ─── Create Booking Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_booking_uses_db_category_price_and_returns_range() -> None:
    customer = _make_user(role="customer")
    cat = _make_category(name="AC Repair", slug="ac_repair", base_price_ngn=11000.0)

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(cat)])

    def _populate_timestamps(obj: object) -> None:
        if isinstance(obj, Booking):
            now = datetime.now(UTC)
            obj.id = uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now

    db.refresh = AsyncMock(side_effect=_populate_timestamps)
    push = MockPushProvider()
    service = BookingService(db=db, push_provider=push)

    payload = CreateBookingRequest(
        category_id=cat.id,
        address="14 Admiralty Way, Lekki Phase 1",
        latitude=6.4474,
        longitude=3.4723,
        urgency="standard",
        duration_hours=2.0,
        matching_mode="manual",
        notes="  Split unit leaking  ",
    )
    res = await service.create_booking(customer.id, payload)

    db.commit.assert_awaited_once()
    assert res.status == "requested"
    assert res.matching_mode == "manual"
    assert res.notes == "Split unit leaking"
    assert res.price_range.currency == "NGN"
    assert res.price_range.confidence == "high"
    assert res.recommended_price is not None
    assert res.recommended_price >= 22000.0  # base 11k * 2h (may have peak multiplier)


@pytest.mark.asyncio
async def test_create_booking_raises_for_missing_category() -> None:
    customer = _make_user(role="customer")
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(None)])
    service = BookingService(db=db, push_provider=MockPushProvider())

    with pytest.raises(CategoryNotFoundError):
        await service.create_booking(
            customer.id,
            CreateBookingRequest(
                category_id=uuid.uuid4(),
                address="14 Admiralty Way, Lekki Phase 1",
                latitude=6.4474,
                longitude=3.4723,
            ),
        )


@pytest.mark.asyncio
async def test_create_booking_forbids_helper_role() -> None:
    helper = _make_user(role="helper")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(helper))
    service = BookingService(db=db, push_provider=MockPushProvider())

    with pytest.raises(ForbiddenError):
        await service.create_booking(
            helper.id,
            CreateBookingRequest(
                category_id=uuid.uuid4(),
                address="14 Admiralty Way, Lekki Phase 1",
                latitude=6.4474,
                longitude=3.4723,
            ),
        )


# ─── Matching Tests (Auto + Manual + Busy Exclusion) ─────────────────────────


@pytest.mark.asyncio
async def test_match_helpers_auto_broadcasts_top_3_and_excludes_busy() -> None:
    customer = _make_user(role="customer")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="requested", matching_mode="auto")

    helpers = [
        _make_user(
            role="helper", full_name=f"Helper {i}", latitude=6.448 + i * 0.005, longitude=3.472
        )
        for i in range(5)
    ]
    profiles = [
        _make_profile(h, categories=[cat], avg_rating=5.0 - i * 0.2) for i, h in enumerate(helpers)
    ]
    busy_helper = helpers[0]  # Closest helper is busy -> must be excluded from matching!
    busy_booking = _make_booking(customer, cat, status="accepted", helper_id=busy_helper.id)

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_result(booking),
            _rows_all_result(list(zip(profiles, helpers, strict=True))),
            _scalars_all_result([busy_booking]),
        ]
    )

    def _assign_offer_ids(obj: object) -> None:
        if isinstance(obj, Booking):
            for offer in obj.offers:
                offer.id = uuid.uuid4()

    db.refresh = AsyncMock(side_effect=_assign_offer_ids)
    push = MockPushProvider()
    service = BookingService(db=db, push_provider=push)

    res = await service.match_helpers(booking.id, customer.id)

    assert res.status == "matched"
    assert res.matching_mode == "auto"
    assert res.notified_count == 3
    assert len(res.offers) == 3
    assert len(push.sent_pushes) == 3
    notified_ids = {o.helper_id for o in res.offers}
    assert busy_helper.id not in notified_ids
    assert all(c.is_busy is False for c in res.candidates)


@pytest.mark.asyncio
async def test_match_helpers_raises_when_all_helpers_busy() -> None:
    customer = _make_user(role="customer")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="requested", matching_mode="auto")

    busy_helper = _make_user(role="helper", latitude=6.448, longitude=3.472)
    busy_profile = _make_profile(busy_helper, categories=[cat])
    busy_booking = _make_booking(customer, cat, status="accepted", helper_id=busy_helper.id)

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_result(booking),
            _rows_all_result([(busy_profile, busy_helper)]),
            _scalars_all_result([busy_booking]),
        ]
    )
    service = BookingService(db=db, push_provider=MockPushProvider())

    with pytest.raises(NoAvailableHelpersError):
        await service.match_helpers(booking.id, customer.id)


@pytest.mark.asyncio
async def test_match_helpers_manual_returns_candidates_with_optional_busy_last() -> None:
    customer = _make_user(role="customer")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="requested", matching_mode="manual")

    busy_helper = _make_user(
        role="helper", full_name="Busy Star", latitude=6.4475, longitude=3.4723
    )
    free_helper = _make_user(
        role="helper", full_name="Free Helper", latitude=6.4600, longitude=3.4723
    )
    busy_profile = _make_profile(busy_helper, categories=[cat])
    free_profile = _make_profile(free_helper, categories=[cat])
    busy_booking = _make_booking(customer, cat, status="accepted", helper_id=busy_helper.id)

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_result(booking),
            _rows_all_result([(busy_profile, busy_helper), (free_profile, free_helper)]),
            _scalars_all_result([busy_booking]),
        ]
    )
    push = MockPushProvider()
    service = BookingService(db=db, push_provider=push)

    res = await service.match_helpers(booking.id, customer.id, include_busy=True)

    assert res.status == "requested"  # Does not transition until select_helper
    assert res.notified_count == 0
    assert len(push.sent_pushes) == 0
    assert [c.full_name for c in res.candidates] == ["Free Helper", "Busy Star"]
    assert res.candidates[0].is_busy is False
    assert res.candidates[1].is_busy is True


@pytest.mark.asyncio
async def test_select_helper_dispatches_single_offer_and_rejects_busy() -> None:
    customer = _make_user(role="customer")
    helper = _make_user(role="helper", latitude=6.448, longitude=3.472)
    cat = _make_category()
    profile = _make_profile(helper, categories=[cat])
    booking = _make_booking(customer, cat, status="requested", matching_mode="manual")

    # Busy case
    busy_booking = _make_booking(customer, cat, status="accepted", helper_id=helper.id)
    db_busy = AsyncMock()
    db_busy.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_result(booking),
            _first_row_result((profile, helper)),
            _scalars_all_result([busy_booking]),
        ]
    )
    with pytest.raises(HelperBusyError):
        await BookingService(db=db_busy, push_provider=MockPushProvider()).select_helper(
            booking.id, customer.id, helper.id
        )

    # Happy path
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_result(booking),
            _first_row_result((profile, helper)),
            _scalars_all_result([]),
        ]
    )

    def _assign_ids(obj: object) -> None:
        if isinstance(obj, Booking):
            for o in obj.offers:
                o.id = uuid.uuid4()

    db.refresh = AsyncMock(side_effect=_assign_ids)
    push = MockPushProvider()
    res = await BookingService(db=db, push_provider=push).select_helper(
        booking.id, customer.id, helper.id
    )
    assert res.status == "matched"
    assert len(res.offers) == 1
    assert res.offers[0].helper_id == helper.id
    assert len(push.sent_pushes) == 1


@pytest.mark.asyncio
async def test_select_helper_raises_when_helper_not_found() -> None:
    customer = _make_user(role="customer")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="requested", matching_mode="manual")

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_result(booking),
            _first_row_result(None),
        ]
    )
    with pytest.raises(HelperNotFoundError):
        await BookingService(db=db, push_provider=MockPushProvider()).select_helper(
            booking.id, customer.id, uuid.uuid4()
        )


# ─── Accept, Decline & Lifecycle Transition Tests ────────────────────────────


@pytest.mark.asyncio
async def test_accept_booking_assigns_helper_and_supersedes_other_offers() -> None:
    customer = _make_user(role="customer")
    h1 = _make_user(role="helper", full_name="Winner")
    h2 = _make_user(role="helper", full_name="Runner Up")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="matched")
    o1 = _make_offer(booking, h1.id, rank=1)
    o2 = _make_offer(booking, h2.id, rank=2)
    booking.offers = [o1, o2]

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(h1),
            _scalar_result(booking),
            MagicMock(),  # HelperProfile FOR UPDATE lock
            _scalars_all_result([]),
        ]
    )
    service = BookingService(db=db, push_provider=MockPushProvider())
    res = await service.accept_booking(booking.id, h1.id)

    assert res.status == "accepted"
    assert res.helper_id == h1.id
    assert res.agreed_price == booking.recommended_price
    assert o1.status == "accepted"
    assert o2.status == "superseded"


@pytest.mark.asyncio
async def test_accept_booking_rejects_already_accepted_and_unoffered_and_busy_race() -> None:
    customer = _make_user(role="customer")
    h1 = _make_user(role="helper")
    h2 = _make_user(role="helper")
    cat = _make_category()

    # Already accepted -> 409
    accepted_booking = _make_booking(customer, cat, status="accepted", helper_id=h1.id)
    db1 = AsyncMock()
    db1.execute = AsyncMock(side_effect=[_scalar_result(h2), _scalar_result(accepted_booking)])
    with pytest.raises(BookingAlreadyAcceptedError):
        await BookingService(db=db1, push_provider=MockPushProvider()).accept_booking(
            accepted_booking.id, h2.id
        )

    # Unoffered helper -> 404
    matched_booking = _make_booking(customer, cat, status="matched")
    matched_booking.offers = [_make_offer(matched_booking, h1.id)]
    db2 = AsyncMock()
    db2.execute = AsyncMock(side_effect=[_scalar_result(h2), _scalar_result(matched_booking)])
    with pytest.raises(BookingOfferNotFoundError):
        await BookingService(db=db2, push_provider=MockPushProvider()).accept_booking(
            matched_booking.id, h2.id
        )

    # DB partial unique index IntegrityError race -> HelperBusyError (409)
    db3 = AsyncMock()
    db3.execute = AsyncMock(
        side_effect=[
            _scalar_result(h1),
            _scalar_result(matched_booking),
            MagicMock(),  # HelperProfile FOR UPDATE lock
            _scalars_all_result([]),
        ]
    )
    db3.commit = AsyncMock(
        side_effect=IntegrityError("stmt", {}, Exception("uq_bookings_active_helper"))
    )
    with pytest.raises(HelperBusyError):
        await BookingService(db=db3, push_provider=MockPushProvider()).accept_booking(
            matched_booking.id, h1.id
        )
    db3.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_decline_booking_reverts_to_requested_when_last_pending_declined() -> None:
    customer = _make_user(role="customer")
    h1 = _make_user(role="helper")
    h2 = _make_user(role="helper")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="matched")
    o1 = _make_offer(booking, h1.id, rank=1)
    o2 = _make_offer(booking, h2.id, rank=2)
    booking.offers = [o1, o2]

    # First helper declines -> stays matched because o2 is still pending
    db1 = AsyncMock()
    db1.execute = AsyncMock(side_effect=[_scalar_result(h1), _scalar_result(booking)])
    res1 = await BookingService(db=db1, push_provider=MockPushProvider()).decline_booking(
        booking.id, h1.id
    )
    assert res1.status == "matched"
    assert o1.status == "declined"

    # Second helper declines -> reverts to requested
    db2 = AsyncMock()
    db2.execute = AsyncMock(side_effect=[_scalar_result(h2), _scalar_result(booking)])
    res2 = await BookingService(db=db2, push_provider=MockPushProvider()).decline_booking(
        booking.id, h2.id
    )
    assert res2.status == "requested"
    assert o2.status == "declined"


@pytest.mark.asyncio
async def test_en_route_start_and_complete_lifecycle_increments_completed_jobs() -> None:
    customer = _make_user(role="customer")
    helper = _make_user(role="helper")
    cat = _make_category()
    profile = _make_profile(helper, completed_jobs_count=4)
    booking = _make_booking(customer, cat, status="accepted", helper_id=helper.id)
    service_db = AsyncMock()

    # accepted -> en_route
    service_db.execute = AsyncMock(side_effect=[_scalar_result(helper), _scalar_result(booking)])
    res = await BookingService(db=service_db, push_provider=MockPushProvider()).mark_en_route(
        booking.id, helper.id
    )
    assert res.status == "en_route"

    # en_route -> in_progress
    service_db.execute = AsyncMock(side_effect=[_scalar_result(helper), _scalar_result(booking)])
    res = await BookingService(db=service_db, push_provider=MockPushProvider()).start_booking(
        booking.id, helper.id
    )
    assert res.status == "in_progress"

    # in_progress -> completed
    service_db.execute = AsyncMock(
        side_effect=[
            _scalar_result(helper),
            _scalar_result(booking),
            _scalar_result(profile),
        ]
    )
    res = await BookingService(db=service_db, push_provider=MockPushProvider()).complete_booking(
        booking.id, helper.id
    )
    assert res.status == "completed"
    assert profile.completed_jobs_count == 5


@pytest.mark.asyncio
async def test_invalid_state_transition_raises_booking_state_error() -> None:
    customer = _make_user(role="customer")
    helper = _make_user(role="helper")
    cat = _make_category()
    # Skipping en_route -> cannot start from accepted
    booking = _make_booking(customer, cat, status="accepted", helper_id=helper.id)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(helper), _scalar_result(booking)])
    with pytest.raises(BookingStateError):
        await BookingService(db=db, push_provider=MockPushProvider()).start_booking(
            booking.id, helper.id
        )


@pytest.mark.asyncio
async def test_cancel_booking_expires_pending_offers_and_blocks_in_progress() -> None:
    customer = _make_user(role="customer")
    helper = _make_user(role="helper")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="matched")
    offer = _make_offer(booking, helper.id)
    booking.offers = [offer]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(booking)])
    res = await BookingService(db=db, push_provider=MockPushProvider()).cancel_booking(
        booking.id, customer.id, reason="Found another date"
    )
    assert res.status == "cancelled"
    assert res.cancellation_reason == "Found another date"
    assert res.cancelled_by == customer.id
    assert offer.status == "expired"

    # Cannot cancel in_progress
    in_prog = _make_booking(customer, cat, status="in_progress", helper_id=helper.id)
    db2 = AsyncMock()
    db2.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(in_prog)])
    with pytest.raises(BookingStateError):
        await BookingService(db=db2, push_provider=MockPushProvider()).cancel_booking(
            in_prog.id, customer.id
        )


# ─── Read / List Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_booking_authorizes_owner_and_blocks_stranger() -> None:
    customer = _make_user(role="customer")
    stranger = _make_user(role="customer")
    cat = _make_category()
    booking = _make_booking(customer, cat)

    db_ok = AsyncMock()
    db_ok.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(booking)])
    res = await BookingService(db=db_ok, push_provider=MockPushProvider()).get_booking(
        booking.id, customer.id
    )
    assert res.id == booking.id

    db_forbidden = AsyncMock()
    db_forbidden.execute = AsyncMock(
        side_effect=[_scalar_result(stranger), _scalar_result(booking)]
    )
    with pytest.raises(ForbiddenError):
        await BookingService(db=db_forbidden, push_provider=MockPushProvider()).get_booking(
            booking.id, stranger.id
        )

    db_missing = AsyncMock()
    db_missing.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(None)])
    with pytest.raises(BookingNotFoundError):
        await BookingService(db=db_missing, push_provider=MockPushProvider()).get_booking(
            uuid.uuid4(), customer.id
        )


@pytest.mark.asyncio
async def test_list_customer_and_helper_bookings_paginated() -> None:
    customer = _make_user(role="customer")
    helper = _make_user(role="helper")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="accepted", helper_id=helper.id)

    db_cust = AsyncMock()
    db_cust.execute = AsyncMock(
        side_effect=[
            _scalar_result(customer),
            _scalar_one_result(1),
            _scalars_all_result([booking]),
        ]
    )
    cust_res = await BookingService(
        db=db_cust, push_provider=MockPushProvider()
    ).list_customer_bookings(customer.id, status="accepted", page=1, page_size=10)
    assert cust_res.total == 1
    assert cust_res.items[0].id == booking.id

    db_helper = AsyncMock()
    db_helper.execute = AsyncMock(
        side_effect=[
            _scalar_result(helper),
            _scalar_one_result(1),
            _scalars_all_result([booking]),
        ]
    )
    helper_res = await BookingService(
        db=db_helper, push_provider=MockPushProvider()
    ).list_helper_bookings(helper.id, page=1, page_size=10)
    assert helper_res.total == 1
    assert helper_res.items[0].id == booking.id


# ─── Router Tests ────────────────────────────────────────────────────────────


def test_bookings_and_helper_queue_routers() -> None:
    customer = _make_user(role="customer")
    helper = _make_user(role="helper")
    cat = _make_category()
    booking = _make_booking(customer, cat, status="accepted", helper_id=helper.id)
    booking_resp = BookingService._to_booking_response(booking)
    paginated = PaginatedBookingsResponse(items=[booking_resp], total=1, page=1, page_size=20)

    mock_service = AsyncMock(spec=BookingService)
    mock_service.list_customer_bookings = AsyncMock(return_value=paginated)
    mock_service.get_booking = AsyncMock(return_value=booking_resp)
    mock_service.mark_en_route = AsyncMock(return_value=booking_resp)
    mock_service.list_helper_bookings = AsyncMock(return_value=paginated)

    now = datetime.now(UTC)
    app.dependency_overrides[get_current_user] = lambda: TokenPayload(
        sub=str(customer.id),
        role="customer",
        iat=now,
        exp=now + timedelta(hours=1),
        type="access",
    )
    app.dependency_overrides[_get_booking_service] = lambda: mock_service
    app.dependency_overrides[_get_helper_booking_service] = lambda: mock_service
    try:
        client = TestClient(app)
        assert client.get("/api/v1/bookings").status_code == 200
        assert client.get(f"/api/v1/bookings/{booking.id}").status_code == 200
        assert client.post(f"/api/v1/bookings/{booking.id}/en-route").status_code == 200
        assert client.get("/api/v1/helpers/me/bookings").status_code == 200
    finally:
        app.dependency_overrides.clear()


# ─── Scheduling, Slot Overlap & Reschedule Tests ─────────────────────────────


def test_scheduled_at_schema_validation_and_utc_normalization() -> None:
    # Aware +01:00 converts to UTC
    future_lagos = (datetime.now(UTC) + timedelta(hours=3)).isoformat().replace("+00:00", "+01:00")
    req = CreateBookingRequest.model_validate(
        {
            "category_id": str(uuid.uuid4()),
            "address": "14 Admiralty Way, Lekki",
            "latitude": 6.4474,
            "longitude": 3.4723,
            "scheduled_at": future_lagos,
        }
    )
    assert req.scheduled_at is not None
    assert req.scheduled_at.tzinfo == UTC

    # Within 5-minute grace window is accepted
    within_grace = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    req_grace = CreateBookingRequest.model_validate(
        {
            "category_id": str(uuid.uuid4()),
            "address": "14 Admiralty Way, Lekki",
            "latitude": 6.4474,
            "longitude": 3.4723,
            "scheduled_at": within_grace,
        }
    )
    assert req_grace.scheduled_at is not None

    # Naive datetime rejected
    with pytest.raises(ValidationError, match="timezone-aware"):
        CreateBookingRequest(
            category_id=uuid.uuid4(),
            address="14 Admiralty Way, Lekki",
            latitude=6.4474,
            longitude=3.4723,
            scheduled_at=datetime(2027, 1, 10, 9, 0),
        )

    # Past > 5 min rejected
    with pytest.raises(ValidationError, match="in the past"):
        CreateBookingRequest(
            category_id=uuid.uuid4(),
            address="14 Admiralty Way, Lekki",
            latitude=6.4474,
            longitude=3.4723,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=10),
        )

    # RescheduleBookingRequest requires concrete future scheduled_at
    with pytest.raises(ValidationError):
        RescheduleBookingRequest.model_validate({"scheduled_at": None})
    with pytest.raises(ValidationError, match="in the past"):
        RescheduleBookingRequest(scheduled_at=datetime.now(UTC) - timedelta(minutes=10))


def test_booking_time_window_and_windows_overlap_with_buffer() -> None:
    now = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
    start_a, end_a = booking_time_window(
        scheduled_at=datetime(2026, 11, 28, 9, 0, tzinfo=UTC),
        created_at=now,
        duration_hours=2.0,
        now=now,
        status="accepted",
    )
    # 09:00-11:00 vs 11:15-12:15 -> overlaps with 30m buffer (11:15 < 11:30)
    start_b, end_b = booking_time_window(
        scheduled_at=datetime(2026, 11, 28, 11, 15, tzinfo=UTC),
        created_at=now,
        duration_hours=1.0,
        now=now,
        status="requested",
    )
    assert windows_overlap(start_a, end_a, start_b, end_b, buffer_minutes=30) is True
    assert windows_overlap(start_a, end_a, start_b, end_b, buffer_minutes=10) is False

    # Boundary: exactly 30m gap (11:30 start) does NOT overlap with 30m buffer
    start_c, end_c = booking_time_window(
        scheduled_at=datetime(2026, 11, 28, 11, 30, tzinfo=UTC),
        created_at=now,
        duration_hours=1.0,
        now=now,
        status="requested",
    )
    assert windows_overlap(start_a, end_a, start_c, end_c, buffer_minutes=30) is False

    # Overrunning in_progress job clamps end to now so immediate ASAP jobs still conflict
    in_prog_start, in_prog_end = booking_time_window(
        scheduled_at=None,
        created_at=now - timedelta(hours=3),
        duration_hours=1.0,
        now=now,
        status="in_progress",
    )
    assert in_prog_start == now - timedelta(hours=3)
    assert in_prog_end == now


@pytest.mark.asyncio
async def test_create_booking_uses_scheduled_at_for_lagos_peak_pricing() -> None:
    customer = _make_user(role="customer")
    cat = _make_category(name="Cleaning", slug="cleaning", base_price_ngn=5000.0)

    def _populate(obj: object) -> None:
        if isinstance(obj, Booking):
            now = datetime.now(UTC)
            obj.id = uuid.uuid4()
            obj.created_at = now
            obj.updated_at = now

    # Future Monday 07:00 UTC == 08:00 WAT (peak) vs Monday 10:00 UTC == 11:00 WAT (off-peak)
    days_ahead = (7 - datetime.now(UTC).weekday()) % 7 or 7
    next_monday = (datetime.now(UTC) + timedelta(days=days_ahead)).replace(
        hour=7, minute=0, second=0, microsecond=0
    )
    peak_slot = next_monday
    off_peak_slot = next_monday.replace(hour=10)

    db_peak = AsyncMock()
    db_peak.add = MagicMock()
    db_peak.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(cat)])
    db_peak.refresh = AsyncMock(side_effect=_populate)
    peak_res = await BookingService(db=db_peak, push_provider=MockPushProvider()).create_booking(
        customer.id,
        CreateBookingRequest(
            category_id=cat.id,
            address="14 Admiralty Way, Lekki Phase 1",
            latitude=6.4474,
            longitude=3.4723,
            scheduled_at=peak_slot,
        ),
    )

    db_off = AsyncMock()
    db_off.add = MagicMock()
    db_off.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(cat)])
    db_off.refresh = AsyncMock(side_effect=_populate)
    off_res = await BookingService(db=db_off, push_provider=MockPushProvider()).create_booking(
        customer.id,
        CreateBookingRequest(
            category_id=cat.id,
            address="14 Admiralty Way, Lekki Phase 1",
            latitude=6.4474,
            longitude=3.4723,
            scheduled_at=off_peak_slot,
        ),
    )

    assert peak_res.scheduled_at == peak_slot
    assert off_res.recommended_price == 5000.0
    assert peak_res.recommended_price == 6000.0  # 1.2x peak multiplier


@pytest.mark.asyncio
async def test_reschedule_booking_transitions_clears_offers_notifies_and_reprices() -> None:
    customer = _make_user(role="customer")
    stranger = _make_user(role="customer")
    helper = _make_user(role="helper")
    cat = _make_category(name="Cleaning", slug="cleaning", base_price_ngn=5000.0)

    days_ahead = (7 - datetime.now(UTC).weekday()) % 7 or 7
    next_monday = (datetime.now(UTC) + timedelta(days=days_ahead)).replace(
        hour=7, minute=0, second=0, microsecond=0
    )

    # 1. Accepted booking -> reverts to requested, releases helper, clears offers, notifies helper
    accepted = _make_booking(customer, cat, status="accepted", helper_id=helper.id)
    accepted.agreed_price = 5000.0
    accepted.offers = [_make_offer(accepted, helper.id, status="accepted")]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(accepted)])
    push = MockPushProvider()
    res = await BookingService(db=db, push_provider=push).reschedule_booking(
        accepted.id, customer.id, next_monday
    )
    assert res.status == "requested"
    assert res.helper_id is None
    assert res.agreed_price is None
    assert res.offers == []
    assert res.scheduled_at == next_monday
    assert res.recommended_price == 6000.0
    assert len(push.sent_pushes) == 1
    assert push.sent_pushes[0]["data"] == {
        "event": "booking.rescheduled",
        "booking_id": str(accepted.id),
    }

    # 2. Requested booking -> stays requested, no push notification
    requested = _make_booking(customer, cat, status="requested")
    db_req = AsyncMock()
    db_req.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(requested)])
    push_req = MockPushProvider()
    res_req = await BookingService(db=db_req, push_provider=push_req).reschedule_booking(
        requested.id, customer.id, next_monday.replace(hour=10)
    )
    assert res_req.status == "requested"
    assert res_req.recommended_price == 5000.0
    assert len(push_req.sent_pushes) == 0

    # 3. Non-owner forbidden
    db_forb = AsyncMock()
    db_forb.execute = AsyncMock(side_effect=[_scalar_result(stranger), _scalar_result(requested)])
    with pytest.raises(ForbiddenError):
        await BookingService(db=db_forb, push_provider=MockPushProvider()).reschedule_booking(
            requested.id, stranger.id, next_monday
        )

    # 4. Disallowed status (en_route) -> BookingStateError
    en_route = _make_booking(customer, cat, status="en_route", helper_id=helper.id)
    db_state = AsyncMock()
    db_state.execute = AsyncMock(side_effect=[_scalar_result(customer), _scalar_result(en_route)])
    with pytest.raises(BookingStateError):
        await BookingService(db=db_state, push_provider=MockPushProvider()).reschedule_booking(
            en_route.id, customer.id, next_monday
        )


def test_bookings_list_filter_validation_and_reschedule_router() -> None:
    customer = _make_user(role="customer")
    cat = _make_category()
    target_slot = datetime.now(UTC) + timedelta(days=1)
    booking = _make_booking(customer, cat, status="requested", scheduled_at=target_slot)
    booking_resp = BookingService._to_booking_response(booking)
    paginated = PaginatedBookingsResponse(items=[booking_resp], total=1, page=1, page_size=20)

    mock_service = AsyncMock(spec=BookingService)
    mock_service.list_customer_bookings = AsyncMock(return_value=paginated)
    mock_service.list_helper_bookings = AsyncMock(return_value=paginated)
    mock_service.reschedule_booking = AsyncMock(return_value=booking_resp)

    now = datetime.now(UTC)
    app.dependency_overrides[get_current_user] = lambda: TokenPayload(
        sub=str(customer.id),
        role="customer",
        iat=now,
        exp=now + timedelta(hours=1),
        type="access",
    )
    app.dependency_overrides[_get_booking_service] = lambda: mock_service
    app.dependency_overrides[_get_helper_booking_service] = lambda: mock_service
    try:
        client = TestClient(app)

        # Valid query params forwarded
        ok = client.get(
            "/api/v1/bookings",
            params={
                "scheduled_from": now.isoformat(),
                "scheduled_to": (now + timedelta(days=7)).isoformat(),
                "sort_by": "created_at",
                "sort_order": "asc",
            },
        )
        assert ok.status_code == 200
        mock_service.list_customer_bookings.assert_awaited_once()

        # Inverted range -> 422 on both customer and helper endpoints
        bad_range = {
            "scheduled_from": (now + timedelta(days=5)).isoformat(),
            "scheduled_to": now.isoformat(),
        }
        assert client.get("/api/v1/bookings", params=bad_range).status_code == 422
        assert client.get("/api/v1/helpers/me/bookings", params=bad_range).status_code == 422

        # Reschedule endpoint
        resched = client.patch(
            f"/api/v1/bookings/{booking.id}/reschedule",
            json={"scheduled_at": target_slot.isoformat()},
        )
        assert resched.status_code == 200
        assert resched.json()["id"] == str(booking.id)
        mock_service.reschedule_booking.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()
