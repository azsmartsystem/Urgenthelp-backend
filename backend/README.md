# UrgentHelp Backend

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
uv run mypy                 # type check app/, tests/ and gunicorn.conf.py (48 files)
uv run mypy app/            # type check app/ only (42 files)
uv run ruff check app/ --fix && uv run ruff format app/  # lint + format
uv run alembic revision --autogenerate -m "description"  # new migration
uv run gunicorn -c gunicorn.conf.py app.main:app         # production server
```

### Type checking notes

`mypy` reuses an incremental cache (`.mypy_cache/`), so it only re-analyzes files
that changed. Pass `--no-incremental` to force a full re-check of every file —
useful after upgrading a dependency, or when you need to rule out a stale cache:

```bash
uv run mypy app/ --no-incremental
```

Both `files` and `disallow_any_explicit` are set in `pyproject.toml`, so bare
`uv run mypy` checks the whole project. Prefer `uv run` over calling `mypy`
directly: it re-syncs dependencies from `uv.lock` first, so you always analyze
against the locked versions rather than a stale virtualenv.

Do not use `mypy -m app` — `-m` resolves the single module named `app`, which is
just `app/__init__.py`, so it reports "1 source file" and silently checks nothing.
Use `uv run mypy`, `uv run mypy app/`, or `uv run mypy -p app` instead.
