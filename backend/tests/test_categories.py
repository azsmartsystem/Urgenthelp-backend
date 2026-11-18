"""Integration tests for Categories API endpoints and seed migration."""

import importlib.util
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.db.session import get_db
from app.engines.pricing import BASE_PRICES
from app.main import app
from app.modules.categories.model import ServiceCategory
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import ClauseElement

_PG_DIALECT: Dialect = cast(Callable[[], Dialect], postgresql.dialect)()

EXPECTED_PRD_SLUGS: frozenset[str] = frozenset(
    {
        "cleaning",
        "laundry",
        "cooking",
        "babysitting",
        "elder_care",
        "office_cleaning",
        "office_assistance",
        "plumbing",
        "electrical_work",
        "carpentry",
        "painting",
        "generator_repair",
        "ac_repair",
        "appliance_repair",
        "gardening",
        "driving",
        "dispatch_services",
        "home_tutoring",
        "pet_care",
    }
)


def _load_seed_migration() -> ModuleType:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20261118_7c4a9e1b2d08_seed_service_categories.py"
    )
    spec = importlib.util.spec_from_file_location("seed_service_categories", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _InMemoryCategoryStore:
    """Fake async DB executor honoring is_active filter and name ascending sort."""

    def __init__(self, rows: list[ServiceCategory] | None = None) -> None:
        self.rows: list[ServiceCategory] = list(rows or [])

    async def execute(self, stmt: ClauseElement) -> MagicMock:
        sql = str(stmt.compile(dialect=_PG_DIALECT))
        filtered = list(self.rows)
        if "service_categories.is_active IS true" in sql:
            filtered = [row for row in filtered if row.is_active]
        if "ORDER BY service_categories.name ASC" in sql:
            filtered.sort(key=lambda row: row.name)

        scalars = MagicMock()
        scalars.all.return_value = filtered
        result = MagicMock()
        result.scalars.return_value = scalars
        return result


@pytest.fixture
async def categories_client() -> AsyncGenerator[tuple[AsyncClient, _InMemoryCategoryStore], None]:
    store = _InMemoryCategoryStore()
    mock_db = AsyncMock()
    mock_db.execute.side_effect = store.execute

    async def override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, store
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_categories_is_public_filters_inactive_and_sorts_by_name(
    categories_client: tuple[AsyncClient, _InMemoryCategoryStore],
) -> None:
    client, store = categories_client
    plumbing_id = uuid.uuid4()
    cleaning_id = uuid.uuid4()
    ac_id = uuid.uuid4()
    store.rows.extend(
        [
            ServiceCategory(
                id=plumbing_id,
                name="Plumbing",
                slug="plumbing",
                description="Pipe and drain repairs.",
                base_price_ngn=8000.0,
                is_active=True,
            ),
            ServiceCategory(
                id=uuid.uuid4(),
                name="Archived Service",
                slug="archived_service",
                description="Should not appear.",
                base_price_ngn=9999.0,
                is_active=False,
            ),
            ServiceCategory(
                id=cleaning_id,
                name="Cleaning",
                slug="cleaning",
                description="Home cleaning.",
                base_price_ngn=5000.0,
                is_active=True,
            ),
            ServiceCategory(
                id=ac_id,
                name="Air-Conditioner Repair",
                slug="ac_repair",
                description=None,
                base_price_ngn=9000.0,
                is_active=True,
            ),
        ]
    )

    resp = await client.get("/api/v1/categories")

    assert resp.status_code == 200
    payload = resp.json()
    assert [item["slug"] for item in payload] == ["ac_repair", "cleaning", "plumbing"]
    assert payload == [
        {
            "id": str(ac_id),
            "name": "Air-Conditioner Repair",
            "slug": "ac_repair",
            "description": None,
            "base_price_ngn": 9000.0,
            "is_active": True,
        },
        {
            "id": str(cleaning_id),
            "name": "Cleaning",
            "slug": "cleaning",
            "description": "Home cleaning.",
            "base_price_ngn": 5000.0,
            "is_active": True,
        },
        {
            "id": str(plumbing_id),
            "name": "Plumbing",
            "slug": "plumbing",
            "description": "Pipe and drain repairs.",
            "base_price_ngn": 8000.0,
            "is_active": True,
        },
    ]

    # Trailing slash also resolves with 200 (no 307 redirect)
    trailing_resp = await client.get("/api/v1/categories/")
    assert trailing_resp.status_code == 200
    assert trailing_resp.json() == payload


@pytest.mark.asyncio
async def test_get_categories_returns_empty_list_when_no_active_categories(
    categories_client: tuple[AsyncClient, _InMemoryCategoryStore],
) -> None:
    client, store = categories_client

    empty_resp = await client.get("/api/v1/categories")
    assert empty_resp.status_code == 200
    assert empty_resp.json() == []

    # Also returns [] when only inactive categories exist
    store.rows.append(
        ServiceCategory(
            id=uuid.uuid4(),
            name="Inactive Only",
            slug="inactive_only",
            description=None,
            base_price_ngn=5000.0,
            is_active=False,
        )
    )
    inactive_resp = await client.get("/api/v1/categories")
    assert inactive_resp.status_code == 200
    assert inactive_resp.json() == []


@pytest.mark.asyncio
async def test_get_categories_serves_all_19_seeded_categories_sorted_by_name(
    categories_client: tuple[AsyncClient, _InMemoryCategoryStore],
) -> None:
    client, store = categories_client
    migration = _load_seed_migration()
    seed_rows: list[dict[str, object]] = migration.SEED_CATEGORIES

    store.rows.extend(
        ServiceCategory(
            id=cast(uuid.UUID, row["id"]),
            name=str(row["name"]),
            slug=str(row["slug"]),
            description=str(row["description"]) if row["description"] is not None else None,
            base_price_ngn=cast(float, row["base_price_ngn"]),
            is_active=bool(row["is_active"]),
        )
        for row in reversed(seed_rows)
    )

    resp = await client.get("/api/v1/categories")

    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 19
    names = [item["name"] for item in payload]
    assert names == sorted(names)
    assert frozenset(item["slug"] for item in payload) == EXPECTED_PRD_SLUGS
    for item in payload:
        assert item["base_price_ngn"] == BASE_PRICES[item["slug"]]
        assert item["is_active"] is True


def test_seed_migration_covers_all_19_prd_categories_and_pricing_parity() -> None:
    migration = _load_seed_migration()
    assert migration.revision == "7c4a9e1b2d08"
    assert migration.down_revision == "30ec41bff66d"

    seed_rows: list[dict[str, object]] = migration.SEED_CATEGORIES
    assert len(seed_rows) == 19

    slugs = [str(row["slug"]) for row in seed_rows]
    names = [str(row["name"]) for row in seed_rows]

    assert frozenset(slugs) == EXPECTED_PRD_SLUGS
    assert len(set(slugs)) == 19
    assert len(set(names)) == 19
    assert all(len(s) <= 50 for s in slugs)
    assert all(len(n) <= 50 for n in names)
    assert all(row["is_active"] is True for row in seed_rows)

    for row in seed_rows:
        slug = str(row["slug"])
        assert slug in BASE_PRICES
        assert row["base_price_ngn"] == BASE_PRICES[slug]


def test_seed_migration_upgrade_and_downgrade_sql_are_idempotent_and_fk_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_seed_migration()
    executed: list[ClauseElement] = []
    monkeypatch.setattr(migration.op, "execute", executed.append)

    migration.upgrade()
    migration.downgrade()

    assert len(executed) == 2
    upgrade_stmt, downgrade_stmt = executed

    upgrade_compiled = upgrade_stmt.compile(dialect=_PG_DIALECT)
    upgrade_sql = str(upgrade_compiled)
    assert "INSERT INTO service_categories" in upgrade_sql
    assert "ON CONFLICT DO NOTHING" in upgrade_sql

    upgrade_params = dict(upgrade_compiled.params)
    bound_slugs: set[str] = set()
    bound_prices_by_slug: dict[str, float] = {}
    for idx in range(len(migration.SEED_CATEGORIES)):
        suffix = f"_m{idx}"
        slug = str(upgrade_params[f"slug{suffix}"])
        price = cast(float, upgrade_params[f"base_price_ngn{suffix}"])
        bound_slugs.add(slug)
        bound_prices_by_slug[slug] = price

    assert bound_slugs == EXPECTED_PRD_SLUGS
    for slug in EXPECTED_PRD_SLUGS:
        assert bound_prices_by_slug[slug] == BASE_PRICES[slug]

    # Simulate idempotent re-run against pre-existing rows (matching slug or name or id)
    existing_by_slug: dict[str, dict[str, object]] = {
        "cleaning": {
            "id": uuid.uuid4(),
            "name": "Cleaning",
            "slug": "cleaning",
            "base_price_ngn": 6500.0,
        }
    }
    for row in migration.SEED_CATEGORIES:
        slug = str(row["slug"])
        if "ON CONFLICT DO NOTHING" in upgrade_sql and slug in existing_by_slug:
            continue
        existing_by_slug[slug] = dict(row)

    assert len(existing_by_slug) == 19
    assert existing_by_slug["cleaning"]["base_price_ngn"] == 6500.0

    downgrade_compiled = downgrade_stmt.compile(
        dialect=_PG_DIALECT, compile_kwargs={"literal_binds": True}
    )
    downgrade_sql = str(downgrade_compiled)
    assert "DELETE FROM service_categories" in downgrade_sql
    assert "service_categories.id IN" in downgrade_sql
    assert "service_categories.slug IN" in downgrade_sql
    assert "NOT (EXISTS (SELECT 1" in downgrade_sql
    assert "FROM bookings" in downgrade_sql
    assert "bookings.category_id = service_categories.id" in downgrade_sql

    for seeded_slug in migration.SEEDED_SLUGS:
        assert f"'{seeded_slug}'" in downgrade_sql
    for seeded_id in migration.SEEDED_IDS:
        assert str(seeded_id) in downgrade_sql or seeded_id.hex in downgrade_sql
