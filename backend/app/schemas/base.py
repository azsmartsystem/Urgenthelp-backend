"""Shared Pydantic schemas and base types used across modules."""

import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class AppBaseModel(BaseModel):
    """Base model for all Pydantic schemas in this project.

    - model_config ensures ORM models can be serialised directly (from_attributes)
    - strict=True means no silent type coercion (int "1" ≠ str "1")
    """

    model_config = ConfigDict(
        from_attributes=True,
        strict=True,
        populate_by_name=True,
    )


class PaginatedResponse(AppBaseModel, Generic[T]):
    """Wrapper for paginated list responses."""

    items: list[T]
    total: int
    page: int
    page_size: int
    has_next: bool


class MessageResponse(AppBaseModel):
    """Simple message response for operations that return no entity."""

    message: str


class UUIDResponse(AppBaseModel):
    """Response returning only a newly created resource ID."""

    id: uuid.UUID


class TimestampedSchema(AppBaseModel):
    """Mixin for schemas that expose created_at / updated_at."""

    created_at: datetime
    updated_at: datetime
