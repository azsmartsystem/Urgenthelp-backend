"""Unit & router tests for HelperService, geospatial helpers, and helper endpoints."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import (
    CategoryNotFoundError,
    ForbiddenError,
    InactiveUserError,
    UserNotFoundError,
)
from app.core.geo import bounding_box, haversine_km
from app.core.security import TokenPayload, get_current_user
from app.main import app
from app.modules.categories.model import ServiceCategory
from app.modules.helpers.model import HelperProfile
from app.modules.helpers.router import _get_service as _get_helper_service
from app.modules.helpers.schemas import DaySchedule, UpdateHelperProfileRequest
from app.modules.helpers.service import HelperService
from app.modules.users.model import User
from fastapi.testclient import TestClient
from pydantic import ValidationError


def _make_user(
    *,
    user_id: uuid.UUID | None = None,
    role: str = "helper",
    is_active: bool = True,
    latitude: float | None = 6.5244,
    longitude: float | None = 3.3792,
    full_name: str = "Tunde Bakare",
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
        address="Yaba, Lagos",
        latitude=latitude,
        longitude=longitude,
        profile_photo_url=None,
    )


def _make_category(name: str = "Plumbing", slug: str = "plumbing") -> ServiceCategory:
    return ServiceCategory(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        description="Pipe repairs",
        base_price_ngn=8000.0,
        is_active=True,
    )


def _make_profile(
    user: User,
    *,
    service_radius_km: float = 10.0,
    is_available: bool = True,
    categories: list[ServiceCategory] | None = None,
) -> HelperProfile:
    profile = HelperProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        bio="Experienced Lagos plumber",
        service_radius_km=service_radius_km,
        is_available=is_available,
        availability_schedule={"mon": {"start": "08:00", "end": "18:00"}},
        certifications=["COREN Certified"],
        avg_rating=4.8,
        completed_jobs_count=12,
    )
    profile.categories = categories or []
    return profile


def _scalar_result(value: object) -> MagicMock:
    res = MagicMock()
    res.scalar_one_or_none.return_value = value
    return res


def _scalars_all_result(values: list[object]) -> MagicMock:
    res = MagicMock()
    res.scalars.return_value.all.return_value = values
    return res


def _rows_all_result(rows: list[tuple[object, ...]]) -> MagicMock:
    res = MagicMock()
    res.all.return_value = rows
    return res


# ─── Geospatial Utility Tests ───────────────────────────────────────────────


def test_haversine_km_lagos_landmarks() -> None:
    # Identical point -> 0 km
    assert haversine_km(6.5244, 3.3792, 6.5244, 3.3792) == pytest.approx(0.0, abs=1e-6)
    # Victoria Island (6.4281, 3.4219) to Ikeja (6.6018, 3.3515) ~ 20.8 km
    dist = haversine_km(6.4281, 3.4219, 6.6018, 3.3515)
    assert 19.5 < dist < 22.0


def test_bounding_box_bounds_and_validation() -> None:
    min_lat, max_lat, min_lon, max_lon = bounding_box(6.5244, 3.3792, 10.0)
    assert min_lat < 6.5244 < max_lat
    assert min_lon < 3.3792 < max_lon

    with pytest.raises(ValueError, match="non-negative"):
        bounding_box(6.5244, 3.3792, -1.0)


# ─── Schema Validation Tests ────────────────────────────────────────────────


def test_day_schedule_rejects_inverted_window() -> None:
    with pytest.raises(ValidationError):
        DaySchedule(start="18:00", end="08:00")


def test_update_helper_profile_rejects_null_required_and_blank_cert() -> None:
    with pytest.raises(ValidationError):
        UpdateHelperProfileRequest(service_radius_km=None)
    with pytest.raises(ValidationError):
        UpdateHelperProfileRequest(certifications=["   "])


# ─── HelperService Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_my_profile_returns_existing() -> None:
    user = _make_user()
    cat = _make_category()
    profile = _make_profile(user, categories=[cat])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(user), _scalar_result(profile)])
    service = HelperService(db=db)

    res = await service.get_my_profile(user.id)
    assert res.user_id == user.id
    assert res.service_radius_km == 10.0
    assert len(res.categories) == 1
    assert res.categories[0].slug == "plumbing"


@pytest.mark.asyncio
async def test_get_my_profile_lazily_creates_default_profile() -> None:
    user = _make_user()
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(user), _scalar_result(None)])

    async def _fake_refresh(obj: HelperProfile) -> None:
        obj.id = uuid.uuid4()
        obj.service_radius_km = 10.0
        obj.is_available = False
        obj.certifications = []
        obj.avg_rating = 0.0
        obj.completed_jobs_count = 0
        obj.categories = []

    db.refresh = AsyncMock(side_effect=_fake_refresh)
    service = HelperService(db=db)

    res = await service.get_my_profile(user.id)
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    assert res.user_id == user.id
    assert res.is_available is False


@pytest.mark.asyncio
async def test_get_my_profile_role_and_status_guards() -> None:
    db = AsyncMock()
    service = HelperService(db=db)

    db.execute = AsyncMock(return_value=_scalar_result(None))
    with pytest.raises(UserNotFoundError):
        await service.get_my_profile(uuid.uuid4())

    inactive = _make_user(is_active=False)
    db.execute = AsyncMock(return_value=_scalar_result(inactive))
    with pytest.raises(InactiveUserError):
        await service.get_my_profile(inactive.id)

    customer = _make_user(role="customer")
    db.execute = AsyncMock(return_value=_scalar_result(customer))
    with pytest.raises(ForbiddenError):
        await service.get_my_profile(customer.id)


@pytest.mark.asyncio
async def test_update_helper_profile_updates_fields_and_categories() -> None:
    user = _make_user()
    profile = _make_profile(user)
    cat1 = _make_category("Plumbing", "plumbing")
    cat2 = _make_category("Cleaning", "cleaning")

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(user),
            _scalar_result(profile),
            _scalars_all_result([cat1, cat2]),
        ]
    )
    service = HelperService(db=db)

    payload = UpdateHelperProfileRequest(
        bio="  Master technician  ",
        service_radius_km=15.0,
        is_available=True,
        availability_schedule={"mon": DaySchedule(start="09:00", end="17:00")},
        certifications=["NABTEB Trade Test"],
        category_ids=[cat1.id, cat2.id, cat1.id],  # duplicate should be deduplicated
    )

    res = await service.update_helper_profile(user.id, payload)
    db.commit.assert_awaited_once()
    assert res.bio == "Master technician"
    assert res.service_radius_km == 15.0
    assert res.certifications == ["NABTEB Trade Test"]
    assert [c.slug for c in res.categories] == ["cleaning", "plumbing"]


@pytest.mark.asyncio
async def test_update_helper_profile_rejects_unknown_category() -> None:
    user = _make_user()
    profile = _make_profile(user)
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _scalar_result(user),
            _scalar_result(profile),
            _scalars_all_result([]),
        ]
    )
    service = HelperService(db=db)

    payload = UpdateHelperProfileRequest(category_ids=[uuid.uuid4()])
    with pytest.raises(CategoryNotFoundError):
        await service.update_helper_profile(user.id, payload)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_helpers_nearby_filters_by_effective_radius_and_sorts() -> None:
    # Search origin: Yaba, Lagos (6.5158, 3.3898)
    close_user = _make_user(full_name="Close Helper", latitude=6.5200, longitude=3.3900)
    mid_user = _make_user(full_name="Mid Helper", latitude=6.5500, longitude=3.3900)
    short_radius_user = _make_user(full_name="Short Radius", latitude=6.5500, longitude=3.3900)

    close_profile = _make_profile(close_user, service_radius_km=10.0)
    mid_profile = _make_profile(mid_user, service_radius_km=10.0)
    # Within search radius (10km), but outside helper's own 2km service radius -> excluded
    short_radius_profile = _make_profile(short_radius_user, service_radius_km=2.0)

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_rows_all_result(
            [
                (mid_profile, mid_user),
                (short_radius_profile, short_radius_user),
                (close_profile, close_user),
            ]
        )
    )
    service = HelperService(db=db)

    results = await service.get_helpers_nearby(
        latitude=6.5158,
        longitude=3.3898,
        radius_km=10.0,
    )
    assert [r.full_name for r in results] == ["Close Helper", "Mid Helper"]
    assert results[0].distance_km < results[1].distance_km


# ─── Router Tests ───────────────────────────────────────────────────────────


def test_nearby_router_rejects_null_island() -> None:
    mock_service = AsyncMock(spec=HelperService)
    app.dependency_overrides[_get_helper_service] = lambda: mock_service
    try:
        client = TestClient(app)
        resp = client.get("/api/v1/helpers/nearby", params={"latitude": 0.0, "longitude": 0.0})
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_get_my_helper_profile_router() -> None:
    user = _make_user()
    profile = _make_profile(user)
    expected = HelperService._to_profile_response(user, profile)

    mock_service = AsyncMock(spec=HelperService)
    mock_service.get_my_profile = AsyncMock(return_value=expected)

    now = datetime.now(UTC)
    app.dependency_overrides[get_current_user] = lambda: TokenPayload(
        sub=str(user.id),
        role="helper",
        iat=now,
        exp=now + timedelta(hours=1),
        type="access",
    )
    app.dependency_overrides[_get_helper_service] = lambda: mock_service
    try:
        client = TestClient(app)
        resp = client.get("/api/v1/helpers/me")
        assert resp.status_code == 200
        assert resp.json()["user_id"] == str(user.id)
    finally:
        app.dependency_overrides.clear()
