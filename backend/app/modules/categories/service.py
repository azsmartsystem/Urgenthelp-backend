"""Category service — business logic for service category catalog discovery.

No HTTP awareness here. Receives typed inputs, returns typed outputs.
"""

import structlog
from app.modules.categories.model import ServiceCategory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class CategoryService:
    """Encapsulates read-only service category catalog queries."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_categories(self, active_only: bool = True) -> list[ServiceCategory]:
        """Return service categories sorted alphabetically by name."""
        stmt = select(ServiceCategory).order_by(ServiceCategory.name.asc())
        if active_only:
            stmt = stmt.where(ServiceCategory.is_active.is_(True))

        result = await self._db.execute(stmt)
        categories = list(result.scalars().all())
        logger.info(
            "categories_listed",
            active_only=active_only,
            count=len(categories),
        )
        return categories
