"""Categories module router — public service category catalog endpoints."""

from typing import Annotated

from app.db.session import get_db
from app.modules.categories.schemas import CategoryResponse
from app.modules.categories.service import CategoryService
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def _get_service(db: Annotated[AsyncSession, Depends(get_db)]) -> CategoryService:
    return CategoryService(db=db)


@router.get("", response_model=list[CategoryResponse], status_code=200)
@router.get("/", response_model=list[CategoryResponse], status_code=200, include_in_schema=False)
async def list_categories(
    service: Annotated[CategoryService, Depends(_get_service)],
) -> list[CategoryResponse]:
    """Return all active service categories ordered alphabetically by name."""
    categories = await service.list_categories(active_only=True)
    return [CategoryResponse.model_validate(category) for category in categories]
