"""Integration tests for Notification Preferences API endpoints."""

import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import app
from app.modules.notifications.model import NotificationPreference
from app.modules.notifications.registry import EVENT_REGISTRY, VALID_CHANNELS, EventClass
from app.modules.users.model import User
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import ClauseElement

_PG_DIALECT: Dialect = cast(Callable[[], Dialect], postgresql.dialect)()


class _InMemoryPreferenceStore:
    """Stateful fake DB executor supporting NotificationPreference SELECT + pg_insert MERGE."""

    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.rows: dict[tuple[uuid.UUID, str, str], NotificationPreference] = {}
        self.write_count: int = 0

    async def get(self, model: type[object], pk: uuid.UUID) -> object | None:
        if model is User:
            return self.users.get(pk)
        return None

    async def execute(self, stmt: ClauseElement) -> MagicMock:
        compiled = stmt.compile(dialect=_PG_DIALECT)
        params: dict[str, object] = dict(compiled.params)
        if getattr(stmt, "is_insert", False):
            sql = str(compiled)
            has_upsert = (
                "ON CONFLICT ON CONSTRAINT uq_notification_pref DO UPDATE" in sql
                and "enabled = excluded.enabled" in sql
                and "updated_at = now()" in sql
            )
            idx = 0
            while f"channel_m{idx}" in params or "channel" in params:
                suffix = f"_m{idx}" if f"channel_m{idx}" in params else ""
                uid = params[f"user_id{suffix}"]
                channel = str(params[f"channel{suffix}"])
                event_type = str(params[f"event_type{suffix}"])
                enabled = bool(params[f"enabled{suffix}"])
                assert isinstance(uid, uuid.UUID)
                key = (uid, channel, event_type)
                if not has_upsert:
                    raise AssertionError(
                        f"Conflicting/insert query missing ON CONFLICT DO UPDATE: {sql}"
                    )
                self.rows[key] = NotificationPreference(
                    id=uuid.uuid4(),
                    user_id=uid,
                    channel=channel,
                    event_type=event_type,
                    enabled=enabled,
                )
                self.write_count += 1
                if not suffix:
                    break
                idx += 1
            return MagicMock()

        # SELECT NotificationPreference rows for user_id
        target_uid = next(
            (v for v in params.values() if isinstance(v, uuid.UUID)),
            None,
        )
        matching = [row for (uid, _, _), row in self.rows.items() if uid == target_uid]
        scalars = MagicMock()
        scalars.all.return_value = matching
        result = MagicMock()
        result.scalars.return_value = scalars
        return result


@pytest.fixture
def test_settings() -> Settings:
    return get_settings()


@pytest.fixture
async def notifications_client(
    test_settings: Settings,
) -> AsyncGenerator[tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock], None]:
    store = _InMemoryPreferenceStore()
    mock_db = AsyncMock()
    mock_db.get.side_effect = store.get
    mock_db.execute.side_effect = store.execute
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()
    mock_db.begin_nested = MagicMock(return_value=AsyncMock())

    async def override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    def override_get_settings() -> Settings:
        return test_settings

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = override_get_settings

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, mock_db

    app.dependency_overrides.clear()


def _make_user(user_id: uuid.UUID, *, is_active: bool = True) -> User:
    return User(
        id=user_id,
        phone="2348011112222",
        email="user@example.com",
        full_name="Test User",
        hashed_password="hashed_password",
        role="customer",
        is_active=is_active,
        is_id_verified=False,
        is_phone_verified=True,
        email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        trust_score=50.0,
    )


def _auth_headers(user_id: uuid.UUID, settings: Settings) -> dict[str, str]:
    token = create_access_token(user_id=user_id, role="customer", settings=settings)
    return {"Authorization": f"Bearer {token}"}


# ─── Auth & User Guard Tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_preferences_require_valid_jwt_and_uuid_sub(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, _, _ = notifications_client

    assert (await client.get("/api/v1/notifications/preferences")).status_code == 401
    assert (
        await client.put("/api/v1/notifications/preferences", json={"preferences": []})
    ).status_code == 401

    now = datetime.now(UTC)
    bad_sub_token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "role": "customer",
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=15),
        },
        test_settings.JWT_ACCESS_SECRET,
        algorithm="HS256",
    )
    resp = await client.get(
        "/api/v1/notifications/preferences",
        headers={"Authorization": f"Bearer {bad_sub_token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_TOKEN"


@pytest.mark.asyncio
async def test_api_preferences_missing_and_inactive_user(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, store, _ = notifications_client

    missing_id = uuid.uuid4()
    missing_headers = _auth_headers(missing_id, test_settings)
    assert (
        await client.get("/api/v1/notifications/preferences", headers=missing_headers)
    ).status_code == 404
    assert (
        await client.put(
            "/api/v1/notifications/preferences",
            json={"preferences": []},
            headers=missing_headers,
        )
    ).status_code == 404

    inactive_id = uuid.uuid4()
    store.users[inactive_id] = _make_user(inactive_id, is_active=False)
    inactive_headers = _auth_headers(inactive_id, test_settings)
    assert (
        await client.get("/api/v1/notifications/preferences", headers=inactive_headers)
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/notifications/preferences",
            json={
                "preferences": [
                    {"channel": "email", "event_type": "booking.matched", "enabled": False}
                ]
            },
            headers=inactive_headers,
        )
    ).status_code == 403
    assert store.write_count == 0


# ─── GET /api/v1/notifications/preferences Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_api_get_preferences_empty_state_returns_defaults_zero_writes(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, store, mock_db = notifications_client
    uid = uuid.uuid4()
    store.users[uid] = _make_user(uid)

    resp = await client.get(
        "/api/v1/notifications/preferences",
        headers=_auth_headers(uid, test_settings),
    )
    assert resp.status_code == 200
    items = resp.json()["preferences"]
    assert len(items) == len(VALID_CHANNELS) * len(EVENT_REGISTRY)
    assert store.write_count == 0
    mock_db.commit.assert_not_awaited()

    by_pair = {(item["channel"], item["event_type"]): item["enabled"] for item in items}
    for channel in VALID_CHANNELS:
        for event_type, event_class in EVENT_REGISTRY.items():
            expected = not (event_class == EventClass.PREFERENTIAL and channel == "sms")
            assert by_pair[(channel, event_type)] is expected


@pytest.mark.asyncio
async def test_api_get_preferences_applies_existing_overrides(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, store, _ = notifications_client
    uid = uuid.uuid4()
    store.users[uid] = _make_user(uid)
    store.rows[(uid, "email", "booking.matched")] = NotificationPreference(
        id=uuid.uuid4(),
        user_id=uid,
        channel="email",
        event_type="booking.matched",
        enabled=False,
    )

    resp = await client.get(
        "/api/v1/notifications/preferences",
        headers=_auth_headers(uid, test_settings),
    )
    assert resp.status_code == 200
    by_pair = {
        (item["channel"], item["event_type"]): item["enabled"]
        for item in resp.json()["preferences"]
    }
    assert by_pair[("email", "booking.matched")] is False
    assert by_pair[("push", "booking.matched")] is True
    assert by_pair[("sms", "booking.matched")] is False


# ─── PUT /api/v1/notifications/preferences Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_api_put_preferences_partial_merge_and_last_write_wins(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, store, _ = notifications_client
    uid = uuid.uuid4()
    store.users[uid] = _make_user(uid)
    headers = _auth_headers(uid, test_settings)

    # First partial PUT: enable SMS booking.offer and disable email booking.matched
    put_1 = await client.put(
        "/api/v1/notifications/preferences",
        json={
            "preferences": [
                {"channel": "sms", "event_type": "booking.offer", "enabled": True},
                {"channel": "email", "event_type": "booking.matched", "enabled": False},
            ]
        },
        headers=headers,
    )
    assert put_1.status_code == 200
    put_1_by_pair = {
        (item["channel"], item["event_type"]): item["enabled"]
        for item in put_1.json()["preferences"]
    }
    assert put_1_by_pair[("sms", "booking.offer")] is True
    assert put_1_by_pair[("email", "booking.matched")] is False
    assert put_1_by_pair[("email", "booking.offer")] is True

    # Second partial PUT: duplicate entries resolve last-write-wins, and flip an
    # already-stored override (email booking.matched -> True) while keeping sms booking.offer.
    put_2 = await client.put(
        "/api/v1/notifications/preferences",
        json={
            "preferences": [
                {"channel": "push", "event_type": "promo.marketing", "enabled": True},
                {"channel": "push", "event_type": "promo.marketing", "enabled": False},
                {"channel": "email", "event_type": "booking.matched", "enabled": True},
            ]
        },
        headers=headers,
    )
    assert put_2.status_code == 200
    put_2_by_pair = {
        (item["channel"], item["event_type"]): item["enabled"]
        for item in put_2.json()["preferences"]
    }
    assert put_2_by_pair[("sms", "booking.offer")] is True
    assert put_2_by_pair[("email", "booking.matched")] is True
    assert put_2_by_pair[("push", "promo.marketing")] is False

    get_resp = await client.get("/api/v1/notifications/preferences", headers=headers)
    assert get_resp.status_code == 200
    by_pair = {
        (item["channel"], item["event_type"]): item["enabled"]
        for item in get_resp.json()["preferences"]
    }
    assert by_pair[("sms", "booking.offer")] is True
    assert by_pair[("email", "booking.matched")] is True
    assert by_pair[("push", "promo.marketing")] is False
    assert by_pair[("email", "booking.offer")] is True


@pytest.mark.asyncio
async def test_api_put_preferences_rejects_invalid_channel_event_and_opt_out_atomically(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, store, mock_db = notifications_client
    uid = uuid.uuid4()
    store.users[uid] = _make_user(uid)
    headers = _auth_headers(uid, test_settings)

    # Unknown channel ("in_app") -> 400 with zero DB writes even if preceded by valid item
    resp_channel = await client.put(
        "/api/v1/notifications/preferences",
        json={
            "preferences": [
                {"channel": "email", "event_type": "booking.matched", "enabled": False},
                {"channel": "in_app", "event_type": "booking.matched", "enabled": True},
            ]
        },
        headers=headers,
    )
    assert resp_channel.status_code == 400
    assert resp_channel.json()["code"] == "NOTIFICATION_INVALID_CHANNEL"
    assert store.write_count == 0

    # Unknown event ("bad.event") -> 400 with zero DB writes
    resp_event = await client.put(
        "/api/v1/notifications/preferences",
        json={
            "preferences": [
                {"channel": "email", "event_type": "bad.event", "enabled": False},
            ]
        },
        headers=headers,
    )
    assert resp_event.status_code == 400
    assert resp_event.json()["code"] == "NOTIFICATION_INVALID_EVENT"
    assert store.write_count == 0

    # Transactional & system_critical opt-out -> 409 with zero DB writes
    for critical_event in ("password.reset", "account.deleted"):
        resp_opt_out = await client.put(
            "/api/v1/notifications/preferences",
            json={
                "preferences": [
                    {"channel": "email", "event_type": "booking.matched", "enabled": False},
                    {"channel": "email", "event_type": critical_event, "enabled": False},
                ]
            },
            headers=headers,
        )
        assert resp_opt_out.status_code == 409
        assert resp_opt_out.json()["code"] == "NOTIFICATION_TRANSACTIONAL_OPTOUT"
        assert store.write_count == 0
        mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_put_preferences_transactional_opt_in_is_noop(
    notifications_client: tuple[AsyncClient, _InMemoryPreferenceStore, AsyncMock],
    test_settings: Settings,
) -> None:
    client, store, mock_db = notifications_client
    uid = uuid.uuid4()
    store.users[uid] = _make_user(uid)
    headers = _auth_headers(uid, test_settings)

    resp = await client.put(
        "/api/v1/notifications/preferences",
        json={
            "preferences": [
                {"channel": "email", "event_type": "password.reset", "enabled": True},
                {"channel": "sms", "event_type": "account.deleted", "enabled": True},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 200
    resp_items = resp.json()["preferences"]
    assert len(resp_items) == len(VALID_CHANNELS) * len(EVENT_REGISTRY)
    resp_by_pair = {(item["channel"], item["event_type"]): item["enabled"] for item in resp_items}
    assert resp_by_pair[("email", "password.reset")] is True
    assert resp_by_pair[("sms", "account.deleted")] is True
    assert store.write_count == 0
    assert len(store.rows) == 0
    mock_db.commit.assert_not_awaited()
