"""Real PostgreSQL + Redis integration tests for Week 3 Booking Lifecycle & Matching."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from app.core.storage import InMemoryStorageBackend
from app.modules.bookings.model import Booking
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _register_and_verify_user(
    client: AsyncClient,
    redis: aioredis.Redis,
    *,
    phone: str,
    email: str,
    full_name: str,
    role: str,
    password: str = "StrongPassword123!",
) -> tuple[str, dict[str, str]]:
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": phone,
            "email": email,
            "full_name": full_name,
            "role": role,
            "password": password,
        },
    )
    assert reg.status_code == 201
    data = reg.json()
    user_id = data["id"]
    canonical_phone = data["phone"]

    sms_otp = await redis.get(f"otp:{canonical_phone}")
    email_otp = await redis.get(f"email_otp:{user_id}")
    assert sms_otp is not None and email_otp is not None

    await client.post("/api/v1/auth/otp/verify", json={"phone": phone, "otp": str(sms_otp)})
    verify_email = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": email, "otp": str(email_otp)},
    )
    assert verify_email.status_code == 200
    access_token = verify_email.json()["access_token"]
    return user_id, {"Authorization": f"Bearer {access_token}"}


async def _setup_available_helper(
    client: AsyncClient,
    redis: aioredis.Redis,
    *,
    phone: str,
    email: str,
    full_name: str,
    latitude: float,
    longitude: float,
    category_ids: list[str],
    service_radius_km: float = 15.0,
) -> tuple[str, dict[str, str]]:
    helper_id, headers = await _register_and_verify_user(
        client,
        redis,
        phone=phone,
        email=email,
        full_name=full_name,
        role="helper",
    )
    loc_res = await client.put(
        "/api/v1/users/me",
        headers=headers,
        json={"address": "Lekki Phase 1, Lagos", "latitude": latitude, "longitude": longitude},
    )
    assert loc_res.status_code == 200

    hp_res = await client.put(
        "/api/v1/helpers/me",
        headers=headers,
        json={
            "bio": f"Verified technician {full_name}",
            "nearest_landmark": "Lekki Toll Gate",
            "service_radius_km": service_radius_km,
            "is_available": True,
            "category_ids": category_ids,
        },
    )
    assert hp_res.status_code == 200
    return helper_id, headers


async def _get_category_by_slug(client: AsyncClient, slug: str) -> dict[str, object]:
    cats = (await client.get("/api/v1/categories")).json()
    return next(c for c in cats if c["slug"] == slug)


@pytest.mark.asyncio
async def test_auto_match_full_lifecycle_and_helper_queue(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Exercise create -> auto match -> accept -> en-route -> start -> complete on real Postgres."""
    client, _, redis, _ = integration_client
    plumbing = await _get_category_by_slug(client, "plumbing")
    plumbing_id = str(plumbing["id"])

    _, customer_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08031110001",
        email="cust.auto@example.com",
        full_name="Ngozi Adeyemi",
        role="customer",
    )
    h1_id, h1_headers = await _setup_available_helper(
        client,
        redis,
        phone="08031110002",
        email="helper1.auto@example.com",
        full_name="Tunde Plumber",
        latitude=6.4480,
        longitude=3.4730,
        category_ids=[plumbing_id],
    )
    h2_id, h2_headers = await _setup_available_helper(
        client,
        redis,
        phone="08031110003",
        email="helper2.auto@example.com",
        full_name="Seyi Plumber",
        latitude=6.4520,
        longitude=3.4750,
        category_ids=[plumbing_id],
    )

    # 1. Create booking
    create_res = await client.post(
        "/api/v1/bookings",
        headers=customer_headers,
        json={
            "category_id": plumbing_id,
            "address": "14 Admiralty Way, Lekki Phase 1, Lagos",
            "latitude": 6.4474,
            "longitude": 3.4723,
            "urgency": "urgent",
            "duration_hours": 2.0,
            "matching_mode": "auto",
            "notes": "Burst kitchen pipe",
        },
    )
    assert create_res.status_code == 201
    created = create_res.json()
    booking_id = created["id"]
    assert created["status"] == "requested"
    assert created["price_range"]["currency"] == "NGN"
    assert created["price_range"]["confidence"] == "high"
    assert created["price_range"]["recommended"] > 0

    # 2. Auto-match broadcasts offers to both helpers
    match_res = await client.post(
        f"/api/v1/bookings/{booking_id}/match",
        headers=customer_headers,
    )
    assert match_res.status_code == 200
    matched = match_res.json()
    assert matched["status"] == "matched"
    assert matched["notified_count"] == 2
    assert {o["helper_id"] for o in matched["offers"]} == {h1_id, h2_id}

    # Both helpers see the pending offer in GET /helpers/me/bookings
    q1 = await client.get("/api/v1/helpers/me/bookings", headers=h1_headers)
    q2 = await client.get("/api/v1/helpers/me/bookings", headers=h2_headers)
    assert q1.status_code == 200 and q1.json()["total"] == 1
    assert q2.status_code == 200 and q2.json()["total"] == 1

    # 3. Helper 1 accepts -> Helper 2's offer superseded & second accept rejected with 409
    accept_res = await client.post(f"/api/v1/bookings/{booking_id}/accept", headers=h1_headers)
    assert accept_res.status_code == 200
    accepted = accept_res.json()
    assert accepted["status"] == "accepted"
    assert accepted["helper_id"] == h1_id
    offer_by_helper = {o["helper_id"]: o["status"] for o in accepted["offers"]}
    assert offer_by_helper[h1_id] == "accepted"
    assert offer_by_helper[h2_id] == "superseded"

    late_accept = await client.post(f"/api/v1/bookings/{booking_id}/accept", headers=h2_headers)
    assert late_accept.status_code == 409
    assert late_accept.json()["code"] == "BOOKING_ALREADY_ACCEPTED"

    # 4. en-route -> cannot cancel from en_route -> start -> complete
    en_route_res = await client.post(f"/api/v1/bookings/{booking_id}/en-route", headers=h1_headers)
    assert en_route_res.status_code == 200
    assert en_route_res.json()["status"] == "en_route"

    bad_cancel = await client.post(
        f"/api/v1/bookings/{booking_id}/cancel", headers=customer_headers
    )
    assert bad_cancel.status_code == 422
    assert bad_cancel.json()["code"] == "BOOKING_INVALID_STATE_TRANSITION"

    start_res = await client.post(f"/api/v1/bookings/{booking_id}/start", headers=h1_headers)
    assert start_res.status_code == 200
    assert start_res.json()["status"] == "in_progress"

    complete_res = await client.post(f"/api/v1/bookings/{booking_id}/complete", headers=h1_headers)
    assert complete_res.status_code == 200
    assert complete_res.json()["status"] == "completed"

    # 5. Helper completed_jobs_count incremented & customer history lists booking
    hp_after = await client.get("/api/v1/helpers/me", headers=h1_headers)
    assert hp_after.status_code == 200
    assert hp_after.json()["completed_jobs_count"] == 1

    cust_list = await client.get("/api/v1/bookings", headers=customer_headers)
    assert cust_list.status_code == 200
    assert cust_list.json()["total"] == 1
    assert cust_list.json()["items"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_manual_match_include_busy_select_helper_and_decline_fallback(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Exercise manual mode with include_busy sorting, HELPER_BUSY rejection, and decline revert."""
    client, _, redis, _ = integration_client
    cleaning = await _get_category_by_slug(client, "cleaning")
    cleaning_id = str(cleaning["id"])

    _, cust1_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08032220001",
        email="cust1.manual@example.com",
        full_name="Kemi Balogun",
        role="customer",
    )
    _, cust2_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08032220002",
        email="cust2.manual@example.com",
        full_name="Damilola Ogun",
        role="customer",
    )
    busy_id, busy_headers = await _setup_available_helper(
        client,
        redis,
        phone="08032220003",
        email="busy.manual@example.com",
        full_name="Busy Close Cleaner",
        latitude=6.4475,
        longitude=3.4724,
        category_ids=[cleaning_id],
    )
    free_id, free_headers = await _setup_available_helper(
        client,
        redis,
        phone="08032220004",
        email="free.manual@example.com",
        full_name="Free Cleaner",
        latitude=6.4600,
        longitude=3.4750,
        category_ids=[cleaning_id],
    )

    # Put busy_id onto an accepted booking first
    b1 = (
        await client.post(
            "/api/v1/bookings",
            headers=cust1_headers,
            json={
                "category_id": cleaning_id,
                "address": "10 Admiralty Way, Lekki Phase 1",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "matching_mode": "manual",
            },
        )
    ).json()
    sel1 = await client.post(
        f"/api/v1/bookings/{b1['id']}/select-helper",
        headers=cust1_headers,
        json={"helper_id": busy_id},
    )
    assert sel1.status_code == 200
    acc1 = await client.post(f"/api/v1/bookings/{b1['id']}/accept", headers=busy_headers)
    assert acc1.status_code == 200

    # Customer 2 creates a manual booking and calls /match?include_busy=true
    b2 = (
        await client.post(
            "/api/v1/bookings",
            headers=cust2_headers,
            json={
                "category_id": cleaning_id,
                "address": "22 Admiralty Way, Lekki Phase 1",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "matching_mode": "manual",
            },
        )
    ).json()
    match_res = await client.post(
        f"/api/v1/bookings/{b2['id']}/match",
        headers=cust2_headers,
        params={"include_busy": True},
    )
    assert match_res.status_code == 200
    m_data = match_res.json()
    assert m_data["status"] == "requested"
    assert [c["helper_id"] for c in m_data["candidates"]] == [free_id, busy_id]
    assert m_data["candidates"][0]["is_busy"] is False
    assert m_data["candidates"][1]["is_busy"] is True

    # Selecting the busy helper fails with 409 HELPER_BUSY
    bad_select = await client.post(
        f"/api/v1/bookings/{b2['id']}/select-helper",
        headers=cust2_headers,
        json={"helper_id": busy_id},
    )
    assert bad_select.status_code == 409
    assert bad_select.json()["code"] == "HELPER_BUSY"

    # Selecting free helper transitions to matched; declining reverts to requested
    ok_select = await client.post(
        f"/api/v1/bookings/{b2['id']}/select-helper",
        headers=cust2_headers,
        json={"helper_id": free_id},
    )
    assert ok_select.status_code == 200
    assert ok_select.json()["status"] == "matched"

    decline_res = await client.post(f"/api/v1/bookings/{b2['id']}/decline", headers=free_headers)
    assert decline_res.status_code == 200
    assert decline_res.json()["status"] == "requested"


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_double_booking_and_unblocks_after_complete(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Prove PostgreSQL uq_bookings_active_helper blocks concurrent active jobs and allows reuse."""
    client, db_session, redis, _ = integration_client
    plumbing = await _get_category_by_slug(client, "plumbing")
    plumbing_id = str(plumbing["id"])

    cust_id, cust_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08033330001",
        email="cust.index@example.com",
        full_name="Femi Lawson",
        role="customer",
    )
    helper_id, helper_headers = await _setup_available_helper(
        client,
        redis,
        phone="08033330002",
        email="helper.index@example.com",
        full_name="Ibrahim Fixer",
        latitude=6.4480,
        longitude=3.4730,
        category_ids=[plumbing_id],
    )

    # Create and match Booking A and Booking B to the same helper
    b_a = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": plumbing_id,
                "address": "1 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "matching_mode": "manual",
            },
        )
    ).json()
    b_b = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": plumbing_id,
                "address": "2 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "matching_mode": "manual",
            },
        )
    ).json()

    assert (
        await client.post(
            f"/api/v1/bookings/{b_a['id']}/select-helper",
            headers=cust_headers,
            json={"helper_id": helper_id},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/bookings/{b_b['id']}/select-helper",
            headers=cust_headers,
            json={"helper_id": helper_id},
        )
    ).status_code == 200

    # Accept Booking A -> succeeds
    assert (
        await client.post(f"/api/v1/bookings/{b_a['id']}/accept", headers=helper_headers)
    ).status_code == 200

    # Accept Booking B via API while A overlaps -> 409 HELPER_BUSY
    conflict = await client.post(f"/api/v1/bookings/{b_b['id']}/accept", headers=helper_headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "HELPER_BUSY"

    # Transition Booking A to en_route -> PostgreSQL partial unique index blocks concurrent en_route
    assert (
        await client.post(f"/api/v1/bookings/{b_a['id']}/en-route", headers=helper_headers)
    ).status_code == 200

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Booking(
                    customer_id=uuid.UUID(cust_id),
                    helper_id=uuid.UUID(helper_id),
                    category_id=uuid.UUID(plumbing_id),
                    status="en_route",
                    urgency="standard",
                    duration_hours=1.0,
                    matching_mode="auto",
                    address="Direct DB insert test",
                    latitude=6.4474,
                    longitude=3.4723,
                )
            )
            await db_session.flush()

    # Complete Booking A -> frees the partial unique index slot
    assert (
        await client.post(f"/api/v1/bookings/{b_a['id']}/start", headers=helper_headers)
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/bookings/{b_a['id']}/complete", headers=helper_headers)
    ).status_code == 200

    # Now accepting Booking B succeeds!
    accept_b = await client.post(f"/api/v1/bookings/{b_b['id']}/accept", headers=helper_headers)
    assert accept_b.status_code == 200
    assert accept_b.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_nearby_helpers_surfaces_is_busy_and_sorts_busy_last(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Confirm GET /helpers/nearby marks committed helpers is_busy=True and sorts them last."""
    client, _, redis, _ = integration_client
    plumbing = await _get_category_by_slug(client, "plumbing")
    plumbing_id = str(plumbing["id"])

    _, cust_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08034440001",
        email="cust.nearby@example.com",
        full_name="Sola Adebayo",
        role="customer",
    )
    busy_id, busy_headers = await _setup_available_helper(
        client,
        redis,
        phone="08034440002",
        email="busy.nearby@example.com",
        full_name="Closer Busy Helper",
        latitude=6.5160,
        longitude=3.3900,
        category_ids=[plumbing_id],
    )
    free_id, _ = await _setup_available_helper(
        client,
        redis,
        phone="08034440003",
        email="free.nearby@example.com",
        full_name="Farther Free Helper",
        latitude=6.5400,
        longitude=3.3900,
        category_ids=[plumbing_id],
    )

    # Commit busy_id to an en_route booking
    booking = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": plumbing_id,
                "address": "Herbert Macaulay Way, Yaba, Lagos",
                "latitude": 6.5158,
                "longitude": 3.3898,
                "matching_mode": "manual",
            },
        )
    ).json()
    await client.post(
        f"/api/v1/bookings/{booking['id']}/select-helper",
        headers=cust_headers,
        json={"helper_id": busy_id},
    )
    await client.post(f"/api/v1/bookings/{booking['id']}/accept", headers=busy_headers)
    await client.post(f"/api/v1/bookings/{booking['id']}/en-route", headers=busy_headers)

    nearby = await client.get(
        "/api/v1/helpers/nearby",
        params={
            "latitude": 6.5158,
            "longitude": 3.3898,
            "radius_km": 10.0,
            "category_id": plumbing_id,
        },
    )
    assert nearby.status_code == 200
    items = nearby.json()
    assert [item["helper_id"] for item in items] == [free_id, busy_id]
    assert items[0]["is_busy"] is False
    assert items[1]["is_busy"] is True


@pytest.mark.asyncio
async def test_cancel_booking_from_matched_expires_pending_offers(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Confirm cancelling a matched booking expires pending offers and records reason/actor."""
    client, _, redis, _ = integration_client
    plumbing = await _get_category_by_slug(client, "plumbing")
    plumbing_id = str(plumbing["id"])

    cust_id, cust_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08035550001",
        email="cust.cancel@example.com",
        full_name="Tope Ade",
        role="customer",
    )
    await _setup_available_helper(
        client,
        redis,
        phone="08035550002",
        email="helper.cancel@example.com",
        full_name="Bola Plumber",
        latitude=6.4480,
        longitude=3.4730,
        category_ids=[plumbing_id],
    )

    booking = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": plumbing_id,
                "address": "14 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
            },
        )
    ).json()
    match_res = await client.post(
        f"/api/v1/bookings/{booking['id']}/match",
        headers=cust_headers,
    )
    assert match_res.status_code == 200

    cancel_res = await client.post(
        f"/api/v1/bookings/{booking['id']}/cancel",
        headers=cust_headers,
        json={"reason": "Issue resolved itself"},
    )
    assert cancel_res.status_code == 200
    cancelled = cancel_res.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "Issue resolved itself"
    assert cancelled["cancelled_by"] == cust_id
    assert all(o["status"] == "expired" for o in cancelled["offers"])


@pytest.mark.asyncio
async def test_scheduled_at_persistence_filtering_and_sorting(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Verify scheduled_at persists, sorts by COALESCE(scheduled_at, created_at), and filters."""
    client, db_session, redis, _ = integration_client
    cleaning = await _get_category_by_slug(client, "cleaning")
    cleaning_id = str(cleaning["id"])

    _, cust_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08036660001",
        email="cust.sched@example.com",
        full_name="Seyi Adeyemi",
        role="customer",
    )

    now = datetime.now(UTC)
    later_slot = (now + timedelta(days=5)).replace(microsecond=0)
    sooner_slot = (now + timedelta(days=2)).replace(microsecond=0)

    # Create ASAP, then far-future, then near-future
    b_asap = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": cleaning_id,
                "address": "1 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
            },
        )
    ).json()
    b_later = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": cleaning_id,
                "address": "2 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "scheduled_at": later_slot.isoformat(),
            },
        )
    ).json()
    b_sooner = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": cleaning_id,
                "address": "3 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "scheduled_at": sooner_slot.isoformat(),
            },
        )
    ).json()

    assert b_asap["scheduled_at"] is None
    assert datetime.fromisoformat(b_later["scheduled_at"]) == later_slot
    assert datetime.fromisoformat(b_sooner["scheduled_at"]) == sooner_slot

    # Separate created_at timestamps because PG now() is constant within one test transaction
    from sqlalchemy import update

    await db_session.execute(
        update(Booking)
        .where(Booking.id == uuid.UUID(b_asap["id"]))
        .values(created_at=now - timedelta(seconds=30))
    )
    await db_session.execute(
        update(Booking)
        .where(Booking.id == uuid.UUID(b_later["id"]))
        .values(created_at=now - timedelta(seconds=20))
    )
    await db_session.execute(
        update(Booking)
        .where(Booking.id == uuid.UUID(b_sooner["id"]))
        .values(created_at=now - timedelta(seconds=10))
    )
    await db_session.flush()

    # Default sort: COALESCE(scheduled_at, created_at) DESC -> later, sooner, asap
    default_list = await client.get("/api/v1/bookings", headers=cust_headers)
    assert default_list.status_code == 200
    assert [i["id"] for i in default_list.json()["items"]] == [
        b_later["id"],
        b_sooner["id"],
        b_asap["id"],
    ]

    # Explicit asc sort -> asap, sooner, later
    asc_list = await client.get(
        "/api/v1/bookings",
        headers=cust_headers,
        params={"sort_by": "scheduled_at", "sort_order": "asc"},
    )
    assert asc_list.status_code == 200
    assert [i["id"] for i in asc_list.json()["items"]] == [
        b_asap["id"],
        b_sooner["id"],
        b_later["id"],
    ]

    # Explicit created_at desc -> sooner (created last), later, asap
    created_desc = await client.get(
        "/api/v1/bookings",
        headers=cust_headers,
        params={"sort_by": "created_at", "sort_order": "desc"},
    )
    assert created_desc.status_code == 200
    assert [i["id"] for i in created_desc.json()["items"]] == [
        b_sooner["id"],
        b_later["id"],
        b_asap["id"],
    ]

    # Range filter including ASAP (COALESCE uses created_at ~ now) up to sooner_slot
    windowed = await client.get(
        "/api/v1/bookings",
        headers=cust_headers,
        params={
            "scheduled_from": (now - timedelta(minutes=5)).isoformat(),
            "scheduled_to": (sooner_slot + timedelta(minutes=1)).isoformat(),
            "sort_order": "asc",
        },
    )
    assert windowed.status_code == 200
    assert [i["id"] for i in windowed.json()["items"]] == [b_asap["id"], b_sooner["id"]]


@pytest.mark.asyncio
async def test_helper_accepts_non_overlapping_slots_and_blocks_buffer_conflict(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Helper can accept two non-overlapping scheduled slots, while overlapping slot is busy."""
    client, _, redis, _ = integration_client
    plumbing = await _get_category_by_slug(client, "plumbing")
    plumbing_id = str(plumbing["id"])

    _, cust_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08037770001",
        email="cust.slots@example.com",
        full_name="Ngozi Eze",
        role="customer",
    )
    helper_id, helper_headers = await _setup_available_helper(
        client,
        redis,
        phone="08037770002",
        email="helper.slots@example.com",
        full_name="Emeka Slots",
        latitude=6.4480,
        longitude=3.4730,
        category_ids=[plumbing_id],
    )
    free_id, _ = await _setup_available_helper(
        client,
        redis,
        phone="08037770003",
        email="helper.slots.free@example.com",
        full_name="Tunde Free",
        latitude=6.4550,
        longitude=3.4750,
        category_ids=[plumbing_id],
    )

    slot_morning = (datetime.now(UTC) + timedelta(days=2)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    slot_afternoon = slot_morning.replace(hour=14)
    slot_overlap = slot_morning.replace(hour=10, minute=15)  # overlaps 09:00-10:00 + 30m buffer

    async def _create_and_select(slot: datetime) -> str:
        res = await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": plumbing_id,
                "address": "10 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "duration_hours": 1.0,
                "matching_mode": "manual",
                "scheduled_at": slot.isoformat(),
            },
        )
        assert res.status_code == 201
        bid = str(res.json()["id"])
        sel = await client.post(
            f"/api/v1/bookings/{bid}/select-helper",
            headers=cust_headers,
            json={"helper_id": helper_id},
        )
        assert sel.status_code == 200
        return bid

    b_morning = await _create_and_select(slot_morning)
    b_afternoon = await _create_and_select(slot_afternoon)

    # Accept both non-overlapping slots
    assert (
        await client.post(f"/api/v1/bookings/{b_morning}/accept", headers=helper_headers)
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/bookings/{b_afternoon}/accept", headers=helper_headers)
    ).status_code == 200

    # Overlapping booking shows helper as is_busy=True in /match?include_busy=true
    overlap_create = await client.post(
        "/api/v1/bookings",
        headers=cust_headers,
        json={
            "category_id": plumbing_id,
            "address": "11 Admiralty Way, Lekki",
            "latitude": 6.4474,
            "longitude": 3.4723,
            "duration_hours": 1.0,
            "matching_mode": "manual",
            "scheduled_at": slot_overlap.isoformat(),
        },
    )
    assert overlap_create.status_code == 201
    b_overlap = str(overlap_create.json()["id"])

    match_overlap = await client.post(
        f"/api/v1/bookings/{b_overlap}/match",
        headers=cust_headers,
        params={"include_busy": True},
    )
    assert match_overlap.status_code == 200
    cands = match_overlap.json()["candidates"]
    assert [c["helper_id"] for c in cands] == [free_id, helper_id]
    assert cands[0]["is_busy"] is False
    assert cands[1]["is_busy"] is True

    # Selecting busy helper for the overlapping slot returns 409 HELPER_BUSY
    sel_overlap = await client.post(
        f"/api/v1/bookings/{b_overlap}/select-helper",
        headers=cust_headers,
        json={"helper_id": helper_id},
    )
    assert sel_overlap.status_code == 409
    assert sel_overlap.json()["code"] == "HELPER_BUSY"


@pytest.mark.asyncio
async def test_reschedule_accepted_booking_releases_helper_reprices_and_allows_rematch(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Rescheduling an accepted booking reverts to requested, clears offers, and allows re-pick."""
    client, _, redis, _ = integration_client
    cleaning = await _get_category_by_slug(client, "cleaning")
    cleaning_id = str(cleaning["id"])

    _, cust_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08038880001",
        email="cust.resched@example.com",
        full_name="Kemi Balogun",
        role="customer",
    )
    helper_id, helper_headers = await _setup_available_helper(
        client,
        redis,
        phone="08038880002",
        email="helper.resched@example.com",
        full_name="Musa Cleaner",
        latitude=6.4480,
        longitude=3.4730,
        category_ids=[cleaning_id],
    )

    days_ahead = (7 - datetime.now(UTC).weekday()) % 7 or 7
    next_monday = (datetime.now(UTC) + timedelta(days=days_ahead)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )  # 11:00 WAT (off-peak)
    peak_monday = next_monday.replace(hour=7)  # 08:00 WAT (peak)

    created = (
        await client.post(
            "/api/v1/bookings",
            headers=cust_headers,
            json={
                "category_id": cleaning_id,
                "address": "22 Admiralty Way, Lekki",
                "latitude": 6.4474,
                "longitude": 3.4723,
                "matching_mode": "manual",
                "scheduled_at": next_monday.isoformat(),
            },
        )
    ).json()
    bid = str(created["id"])
    off_peak_price = float(created["recommended_price"])

    assert (
        await client.post(
            f"/api/v1/bookings/{bid}/select-helper",
            headers=cust_headers,
            json={"helper_id": helper_id},
        )
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/bookings/{bid}/accept", headers=helper_headers)
    ).status_code == 200

    resched = await client.patch(
        f"/api/v1/bookings/{bid}/reschedule",
        headers=cust_headers,
        json={"scheduled_at": peak_monday.isoformat()},
    )
    assert resched.status_code == 200
    body = resched.json()
    assert body["status"] == "requested"
    assert body["helper_id"] is None
    assert body["agreed_price"] is None
    assert body["offers"] == []
    assert datetime.fromisoformat(body["scheduled_at"]) == peak_monday
    assert float(body["recommended_price"]) > off_peak_price

    # Re-selecting and re-accepting the exact same helper must succeed without IntegrityError
    assert (
        await client.post(
            f"/api/v1/bookings/{bid}/select-helper",
            headers=cust_headers,
            json={"helper_id": helper_id},
        )
    ).status_code == 200
    reaccept = await client.post(f"/api/v1/bookings/{bid}/accept", headers=helper_headers)
    assert reaccept.status_code == 200
    assert reaccept.json()["helper_id"] == helper_id
