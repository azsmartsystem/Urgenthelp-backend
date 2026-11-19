"""Real PostgreSQL + Redis integration tests for the Auth module."""

import pytest
import redis.asyncio as aioredis
from app.core.storage import InMemoryStorageBackend
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_auth_full_lifecycle_register_verify_login_refresh_logout(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Exercise register -> verify -> login -> refresh -> logout on real Postgres + Redis."""
    client, _, redis, _ = integration_client

    # 1. Phone pre-check before registration -> is_registered=False
    check_before = await client.post("/api/v1/auth/phone/check", json={"phone": "0803 111 2233"})
    assert check_before.status_code == 200
    assert check_before.json() == {"phone": "2348031112233", "is_registered": False}

    # 2. Register new customer (persists real row in Postgres + OTPs in Redis)
    reg_res = await client.post(
        "/api/v1/auth/register",
        json={
            "phone": "08031112233",
            "email": "integration.user@example.com",
            "full_name": "Adaeze Okafor",
            "role": "customer",
            "password": "StrongPassword123!",
        },
    )
    assert reg_res.status_code == 201
    reg_data = reg_res.json()
    user_id = reg_data["id"]
    assert reg_data["phone"] == "2348031112233"

    # 3. Phone pre-check now reports registered=True from real Postgres
    check_after = await client.post("/api/v1/auth/phone/check", json={"phone": "+2348031112233"})
    assert check_after.status_code == 200
    assert check_after.json() == {"phone": "2348031112233", "is_registered": True}

    # 4. Verify phone OTP first -> still blocked until email is also verified
    sms_otp = await redis.get("otp:2348031112233")
    assert sms_otp is not None
    phone_verify_res = await client.post(
        "/api/v1/auth/otp/verify",
        json={"phone": "+2348031112233", "otp": str(sms_otp)},
    )
    assert phone_verify_res.status_code == 403
    assert phone_verify_res.json()["code"] == "AUTH_EMAIL_NOT_VERIFIED"

    # 5. Verify email OTP -> both verified, tokens issued!
    email_otp = await redis.get(f"email_otp:{user_id}")
    assert email_otp is not None
    email_verify_res = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "integration.user@example.com", "otp": str(email_otp)},
    )
    assert email_verify_res.status_code == 200
    tokens = email_verify_res.json()
    assert "access_token" in tokens and "refresh_token" in tokens

    # 6. Login with password against real Postgres bcrypt hash
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"phone": "08031112233", "password": "StrongPassword123!"},
    )
    assert login_res.status_code == 200
    login_tokens = login_res.json()

    # 7. Refresh rotates token & blacklists old refresh token in real Redis
    refresh_res = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login_tokens["refresh_token"]},
    )
    assert refresh_res.status_code == 200
    rotated = refresh_res.json()

    # Reusing the old refresh token fails with 401
    replay_res = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login_tokens["refresh_token"]},
    )
    assert replay_res.status_code == 401

    # 8. Logout revokes the rotated refresh token in real Redis
    logout_res = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": rotated["refresh_token"]},
    )
    assert logout_res.status_code == 204

    post_logout_refresh = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": rotated["refresh_token"]},
    )
    assert post_logout_refresh.status_code == 401


@pytest.mark.asyncio
async def test_phone_check_real_redis_sliding_window_rate_limit(
    integration_client: tuple[AsyncClient, AsyncSession, aioredis.Redis, InMemoryStorageBackend],
) -> None:
    """Verify atomic Redis ZSET sliding-window rate limit on /auth/phone/check."""
    client, _, _, _ = integration_client
    headers = {"x-forwarded-for": "203.0.113.77"}

    for _ in range(10):
        res = await client.post(
            "/api/v1/auth/phone/check",
            json={"phone": "08090001122"},
            headers=headers,
        )
        assert res.status_code == 200

    blocked = await client.post(
        "/api/v1/auth/phone/check",
        json={"phone": "08090001122"},
        headers=headers,
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "AUTH_PHONE_CHECK_RATE_LIMITED"
