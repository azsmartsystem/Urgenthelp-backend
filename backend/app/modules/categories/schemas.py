"""Pydantic schemas for the categories module."""

import uuid

from app.schemas.base import AppBaseModel


class CategoryResponse(AppBaseModel):
    """Public read-only representation of a service category."""

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    base_price_ngn: float
    is_active: bool
