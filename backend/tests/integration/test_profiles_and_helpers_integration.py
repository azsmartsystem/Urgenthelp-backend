"""Real PostgreSQL + Redis integration tests for Categories, User Profiles, Helpers & ID Upload."""

import pytest
import redis.asyncio as aioredis
from app.core.storage import InMemoryStorageBackend
from httpx import AsyncClient
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


@pytest.mark.asyncio
async def test_seeded_categories_returned_from_real_postgres(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Confirm Alembic seed migration populated canonical service categories."""
    client, _, _, _ = integration_client
    res = await client.get("/api/v1/categories")
    assert res.status_code == 200
    categories = res.json()
    slugs = {c["slug"] for c in categories}
    assert len(categories) >= 16
    assert {"cleaning", "plumbing", "electrical_work", "ac_repair"}.issubset(slugs)


@pytest.mark.asyncio
async def test_user_profile_update_and_identity_verification_upload(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Exercise GET/PUT /users/me and POST /users/verify-identity against real Postgres."""
    client, _, redis, storage = integration_client
    user_id, auth_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08051234567",
        email="customer.profile@example.com",
        full_name="Chidi Nwosu",
        role="customer",
    )

    update_res = await client.put(
        "/api/v1/users/me",
        headers=auth_headers,
        json={
            "full_name": "Chidi K. Nwosu",
            "address": "14 Herbert Macaulay Way, Yaba, Lagos",
            "latitude": 6.5158,
            "longitude": 3.3898,
        },
    )
    assert update_res.status_code == 200
    assert update_res.json()["full_name"] == "Chidi K. Nwosu"
    assert update_res.json()["latitude"] == pytest.approx(6.5158)

    # Upload real PDF magic bytes for identity verification
    pdf_bytes = b"%PDF-1.7\n%NIN slip\n"
    verify_res = await client.post(
        "/api/v1/users/verify-identity",
        headers=auth_headers,
        data={"document_type": "nin"},
        files={"file": ("nin.pdf", pdf_bytes, "application/pdf")},
    )
    assert verify_res.status_code == 201
    v_data = verify_res.json()
    assert v_data["user_id"] == user_id
    assert v_data["document_type"] == "nin"
    assert v_data["status"] == "pending"
    assert v_data["document_url"] in storage._objects

    # Confirm user profile reflects is_id_verified=False
    me_res = await client.get("/api/v1/users/me", headers=auth_headers)
    assert me_res.status_code == 200
    assert me_res.json()["is_id_verified"] is False


@pytest.mark.asyncio
async def test_helper_profile_categories_and_nearby_geospatial_query(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Exercise helper_profiles JSONB, helper_categories FK join, and GET /helpers/nearby."""
    client, _, redis, _ = integration_client

    cats = (await client.get("/api/v1/categories")).json()
    plumbing = next(c for c in cats if c["slug"] == "plumbing")
    cleaning = next(c for c in cats if c["slug"] == "cleaning")

    helper_id, helper_headers = await _register_and_verify_user(
        client,
        redis,
        phone="08069876543",
        email="helper.lagos@example.com",
        full_name="Emeka Eze",
        role="helper",
    )

    # Set helper location in Yaba, Lagos
    loc_res = await client.put(
        "/api/v1/users/me",
        headers=helper_headers,
        json={"address": "Yaba, Lagos", "latitude": 6.5200, "longitude": 3.3900},
    )
    assert loc_res.status_code == 200

    # Lazily create + update helper profile with categories, schedule, radius, certifications
    hp_res = await client.put(
        "/api/v1/helpers/me",
        headers=helper_headers,
        json={
            "bio": "Certified Lagos plumber with 6 years experience",
            "service_radius_km": 12.0,
            "is_available": True,
            "availability_schedule": {"mon": {"start": "08:00", "end": "18:00"}},
            "certifications": ["COREN Trade Cert"],
            "category_ids": [plumbing["id"]],
        },
    )
    assert hp_res.status_code == 200
    hp_data = hp_res.json()
    assert hp_data["user_id"] == helper_id
    assert hp_data["is_available"] is True
    assert [c["slug"] for c in hp_data["categories"]] == ["plumbing"]

    # Nearby query matching plumbing category finds our helper
    nearby_match = await client.get(
        "/api/v1/helpers/nearby",
        params={
            "latitude": 6.5158,
            "longitude": 3.3898,
            "radius_km": 10.0,
            "category_id": plumbing["id"],
        },
    )
    assert nearby_match.status_code == 200
    found = nearby_match.json()
    assert len(found) == 1
    assert found[0]["helper_id"] == helper_id
    assert found[0]["distance_km"] < 1.0

    # Nearby query for cleaning category excludes this plumbing-only helper
    nearby_miss = await client.get(
        "/api/v1/helpers/nearby",
        params={
            "latitude": 6.5158,
            "longitude": 3.3898,
            "radius_km": 10.0,
            "category_id": cleaning["id"],
        },
    )
    assert nearby_miss.status_code == 200
    assert nearby_miss.json() == []
