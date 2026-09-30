# ALLinHELP Backend

AI-assisted local services marketplace API built with FastAPI.

## Stack

- **Framework:** FastAPI (async)
- **Database:** PostgreSQL + SQLAlchemy 2.0 (async)
- **Cache:** Redis
- **Package manager:** uv

## Quickstart

```bash
cp .env.example .env        # fill in your values
uv sync --dev               # install dependencies
uv run alembic upgrade head # run migrations
uv run uvicorn app.main:app --reload --port 8000
```

Docs available at `http://localhost:8000/docs` (dev only).

## Commands

```bash
uv run pytest               # run tests
uv run pytest --cov         # with coverage
uv run mypy app/            # type check
uv run ruff check app/ --fix && uv run ruff format app/  # lint + format
uv run alembic revision --autogenerate -m "description"  # new migration
uv run gunicorn -c gunicorn.conf.py app.main:app         # production server
```
