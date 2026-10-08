"""Alembic environment configuration for async SQLAlchemy."""

import asyncio
from logging.config import fileConfig

# Import every model module so Base.metadata is complete before autogenerate
# runs. Models only register themselves via import side-effects, so an unimported
# module is invisible to autogenerate — it will report "no new upgrade
# operations" while the table is missing from the database. Add a new import
# here whenever you add a model.py.
import app.modules.bookings.model
import app.modules.categories.model
import app.modules.notifications.model
import app.modules.users.model  # noqa: F401
from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_db_url = str(settings.DATABASE_URL).replace("postgresql://", "postgresql+asyncpg://", 1)


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_db_url)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
