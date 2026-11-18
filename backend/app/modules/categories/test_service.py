"""Unit tests for CategoryService and CategoryResponse schema."""

import uuid
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.modules.categories.model import ServiceCategory
from app.modules.categories.schemas import CategoryResponse
from app.modules.categories.service import CategoryService
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql import ClauseElement

_PG_DIALECT: Dialect = cast(Callable[[], Dialect], postgresql.dialect)()


def _make_category(
    name: str = "Cleaning",
    slug: str = "cleaning",
    description: str | None = "Standard home cleaning.",
    base_price_ngn: float = 5000.0,
    is_active: bool = True,
) -> ServiceCategory:
    return ServiceCategory(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        description=description,
        base_price_ngn=base_price_ngn,
        is_active=is_active,
    )


def _mock_db_returning(rows: list[ServiceCategory]) -> tuple[AsyncMock, list[ClauseElement]]:
    executed_stmts: list[ClauseElement] = []

    async def fake_execute(stmt: ClauseElement) -> MagicMock:
        executed_stmts.append(stmt)
        scalars = MagicMock()
        scalars.all.return_value = rows
        result = MagicMock()
        result.scalars.return_value = scalars
        return result

    db = AsyncMock()
    db.execute.side_effect = fake_execute
    return db, executed_stmts


@pytest.mark.asyncio
async def test_list_categories_active_only_filters_and_orders_by_name() -> None:
    categories = [
        _make_category(name="Babysitting", slug="babysitting", base_price_ngn=4000.0),
        _make_category(name="Cleaning", slug="cleaning", base_price_ngn=5000.0),
    ]
    db, stmts = _mock_db_returning(categories)
    service = CategoryService(db=db)

    result = await service.list_categories()

    assert result == categories
    assert len(stmts) == 1
    sql = str(stmts[0].compile(dialect=_PG_DIALECT))
    assert "FROM service_categories" in sql
    assert "service_categories.is_active IS true" in sql
    assert "ORDER BY service_categories.name ASC" in sql


@pytest.mark.asyncio
async def test_list_categories_active_only_false_omits_active_filter() -> None:
    categories = [
        _make_category(name="Cleaning", slug="cleaning", is_active=True),
        _make_category(name="Legacy", slug="legacy", is_active=False),
    ]
    db, stmts = _mock_db_returning(categories)
    service = CategoryService(db=db)

    result = await service.list_categories(active_only=False)

    assert result == categories
    assert len(stmts) == 1
    sql = str(stmts[0].compile(dialect=_PG_DIALECT))
    assert "FROM service_categories" in sql
    assert "WHERE" not in sql
    assert "ORDER BY service_categories.name ASC" in sql


@pytest.mark.asyncio
async def test_list_categories_returns_empty_list_when_none_found() -> None:
    db, _ = _mock_db_returning([])
    service = CategoryService(db=db)

    result = await service.list_categories()

    assert result == []


def test_category_response_serializes_orm_with_strict_validation() -> None:
    category = _make_category(
        name="Laundry",
        slug="laundry",
        description=None,
        base_price_ngn=3000.0,
        is_active=True,
    )

    schema = CategoryResponse.model_validate(category)

    assert schema.id == category.id
    assert schema.name == "Laundry"
    assert schema.slug == "laundry"
    assert schema.description is None
    assert schema.base_price_ngn == 3000.0
    assert schema.is_active is True


def test_category_response_strict_mode_rejects_coerced_types() -> None:
    with pytest.raises(ValidationError):
        CategoryResponse(
            id=str(uuid.uuid4()),  # type: ignore[arg-type]  # testing strict UUID enforcement
            name="Cleaning",
            slug="cleaning",
            description=None,
            base_price_ngn=5000.0,
            is_active=True,
        )
