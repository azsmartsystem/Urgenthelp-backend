# AGENTS.md — UrgentHelp Backend

This file defines the rules, conventions, and architectural decisions for the UrgentHelp FastAPI backend.
Read this entire file before writing, editing, or reviewing any code in this project.

---

## Project Overview

UrgentHelp is a Solana-inspired, AI-assisted marketplace for local services. The backend is responsible for:

- Phone + OTP authentication with JWT
- User and Helper profile management (single unified user model, role-based)
- Service category management (admin-configurable)
- Booking lifecycle management with a strict state machine
- AI engine orchestration (Matching, Pricing, Trust, Fraud — rule-based at MVP, ML-ready)
- Payment processing and wallet management via Paystack
- Real-time in-app messaging via WebSockets
- Push notifications via Firebase Cloud Messaging
- Identity verification (government ID upload + admin review)
- Admin dashboard APIs

**Runtime:** Python 3.12+
**Package Manager:** uv (never pip directly)
**Framework:** FastAPI (async)
**ASGI Server:** Uvicorn (dev) / Gunicorn + UvicornWorker (production)
**Database:** PostgreSQL via async SQLAlchemy 2.0 + asyncpg
**Migrations:** Alembic
**Cache:** Redis
**Language:** Python with strict mypy (no `Any` — ever)

---

## Python & Typing Rules

### No `Any` — ever

Enforced by ruff `ANN401`, not mypy (see [below](#why-disallow_any_explicit-stays-false)
for why mypy's `disallow_any_explicit` cannot be used with Pydantic).

```python
# ❌ Never do this
def process(data: Any) -> Any: ...


result: Any = await some_call()


# ✅ Do this instead
def process(data: BookingRequest) -> BookingResult: ...


result: BookingResult = await some_call()
```

If the shape of external data is unknown (e.g. raw API responses), use `object` or `Unknown`-style narrowing:

```python
# ✅ Use TypeGuard to narrow unknown external data
from typing import TypeGuard


def is_paystack_event(raw: object) -> TypeGuard[PaystackEvent]:
    return isinstance(raw, dict) and "event" in raw and "data" in raw


raw = response.json()
if not is_paystack_event(raw):
    raise ExternalServiceError(context={"raw": str(raw)})
```

### mypy strict mode — always

`pyproject.toml` must always have:

```toml
[tool.mypy]
files = ["app", "tests", "gunicorn.conf.py"]
strict = true
warn_unreachable = true
```

`files` makes bare `uv run mypy` check everything; passing a path overrides it
(`mypy app/modules/bookings`). Note `mypy -m app` checks only `app/__init__.py`
and follows no imports — use `mypy app`, `mypy -p app`, or bare `mypy` instead.

#### Why `disallow_any_explicit` stays `false`

`disallow_any_explicit = true` **cannot** be used with Pydantic v2. Pydantic's
`BaseModel` declares `__pydantic_extra__: dict[str, Any] | None` and
`__init__(self, /, **data: Any)`, and mypy attributes those inherited `Any`
annotations to *every* subclass. Each Pydantic model in the codebase therefore
raises `Explicit "Any" is not allowed` on its `class Foo(BaseModel):` line, even
though no `Any` appears in our source.

The "no `Any` — ever" rule is therefore enforced by **ruff `ANN401`**
(`any-type`), which is already active via the `ANN` select in `[tool.ruff.lint]`
and flags only `Any` written in our own annotations. Run `uv run ruff check app/`
to audit it. If you ever need mypy's own check, drop
`plugins = ["pydantic.mypy"]` as well — but that costs correct `BaseSettings`
validation and produces 11 spurious `call-arg` errors on `Settings()`.

Never add `# type: ignore` without a comment explaining why, and never to silence a legitimate type error. Fix the code instead.

---

## Validation — Pydantic v2 Only

All incoming data (request bodies, query params, settings) is validated using **Pydantic v2**. Do not use `marshmallow`, `cerberus`, `voluptuous`, or manual dict assertions.

### Defining schemas

```python
# app/modules/bookings/schemas.py
from typing import Literal
from pydantic import Field, field_validator
from app.schemas.base import AppBaseModel


class CreateBookingRequest(AppBaseModel):
    category: str = Field(min_length=2, max_length=50)
    address: str = Field(min_length=5, max_length=500)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    urgency: Literal["standard", "urgent"] = "standard"
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("category")
    @classmethod
    def normalise_category(cls, v: str) -> str:
        return v.lower().strip()
```

### All schemas inherit AppBaseModel

```python
# app/schemas/base.py
class AppBaseModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,  # ORM → schema serialisation
        strict=True,  # no silent coercion
        populate_by_name=True,
    )
```

FastAPI automatically validates request bodies — you do not need a separate validation pipe. The Pydantic model IS the validation.

---

## Module Architecture

Every feature module follows this exact structure — no exceptions:

```markdown
app/modules/<feature>/
├── router.py       — HTTP only: routing, calling service, returning response
├── service.py      — business logic only: no HTTP awareness
├── model.py        — SQLAlchemy ORM model (if module owns a table)
├── schemas.py      — Pydantic request + response schemas
├── repository.py   — DB queries (optional: for complex query logic)
└── test_service.py — pytest unit tests for the service
```

**Routers** handle HTTP concerns only — parse the request, call a service method, return the result. Zero business logic.

**Services** contain all business logic. They receive typed inputs and return typed outputs. They raise typed exceptions. They are unaware of HTTP.

**Repositories** contain complex DB query logic when service.py gets cluttered. Simple queries (findById, etc.) can live directly in the service.

**Never** import a router into another module. Only services/repositories are shared.

---

## Error Handling

### Custom exception hierarchy

Every domain error must be a typed dataclass exception. Never raise bare `Exception`, `ValueError`, or `HTTPException` with a plain string.

```python
# app/core/exceptions.py
@dataclass
class BookingNotFoundError(UrgentHelpError):
    code: str = "BOOKING_NOT_FOUND"
    detail: str = "Booking not found."
    status_code: int = status.HTTP_404_NOT_FOUND
```

Usage in service:

```python
booking = await self._db.get(Booking, booking_id)
if booking is None:
    raise BookingNotFoundError(context={"booking_id": str(booking_id)})
```

The global exception handler in `app/core/exceptions.py` converts all `UrgentHelpError` subclasses into consistent JSON:

```json
{
  "code": "BOOKING_NOT_FOUND",
  "detail": "Booking not found.",
  "path": "/api/v1/bookings/abc-123"
}
```

---

## Logging

Use `structlog` — never `print()`, `logging.info()`, or `console.log()`.

```python
# ❌ Never
print(f"Booking created: {booking_id}")

# ✅ Always
import structlog

logger = structlog.get_logger(__name__)
logger.info("booking_created", booking_id=str(booking_id), customer_id=str(customer_id))
```

### Log levels

| Situation | Level |
| ----------- | ------- |
| Normal operation, significant events | `info` |
| Business rule violations (no helpers found, insufficient balance) | `warning` |
| Unexpected errors, external API failures | `error` |
| Detailed flow tracing (dev only) | `debug` |
| Startup, shutdown, config loaded | `info` with event name |

### Structured log format

```python
logger.info(
    "booking_matched",
    booking_id=str(booking.id),
    helper_id=str(helper.id),
    match_score=round(score, 3),
    duration_ms=int((time.monotonic() - start) * 1000),
)

logger.warning(
    "no_helpers_available",
    category=booking.category,
    radius_km=settings.DEFAULT_MATCH_RADIUS_KM,
    customer_id=str(booking.customer_id)[:8] + "...",  # truncate in prod
)
```

### Never log

- Full phone numbers in production (truncate to last 4 digits)
- JWT tokens or refresh tokens
- Full request bodies (may contain PII / payment data)
- Raw webhook payloads from payment providers

---

## AI Engines

All rule-based AI engines live in `app/engines/`. Each engine has a clean typed interface:

```python
# Clean input/output dataclasses — never pass raw dicts
def rank_helpers(
    candidates: list[HelperCandidate], job: JobRequest, top_n: int = 5
) -> list[RankedHelper]: ...
def recommend_price(request: PricingRequest) -> PriceRange: ...
def calculate_trust_score(data: TrustInput) -> float: ...
def run_fraud_check(ctx: FraudContext) -> list[FraudFlag]: ...
```

**Rule:** Engine functions are pure functions — no DB access, no HTTP calls, no side effects. They receive data, compute results, return results. This makes them trivially testable and swappable for ML models later.

---

## Booking State Machine

The booking status transitions are encoded in `Booking.VALID_TRANSITIONS`. Always use `booking.can_transition_to()` before any status update:

```python
# ✅ Correct
if not booking.can_transition_to("completed"):
    raise BookingStateError(
        context={"current": booking.status, "attempted": "completed"}
    )
booking.status = "completed"

# ❌ Never
booking.status = "completed"  # bypasses validation
```

Valid transitions:

```markdown
requested → matched → accepted → en_route → in_progress → completed
                ↘ cancelled      ↘ cancelled                ↘ disputed → completed/cancelled
```

---

## Database Rules

- All DB access is async via `AsyncSession` — never use sync SQLAlchemy
- Always use `get_db()` FastAPI dependency — never create sessions manually
- Never use `session.execute()` with raw SQL strings — use SQLAlchemy ORM or `text()` only for documented edge cases
- All schema changes require an Alembic migration — never modify tables directly
- Use `mapped_column()` with explicit types — no implicit columns

---

## Solana / Payments

All payment interactions go through `app/modules/payments/service.py`. No other service creates Paystack clients directly.

```python
# ✅ Correct — inject PaymentService
class BookingService:
    def __init__(self, db: AsyncSession, payment_service: PaymentService) -> None: ...


# ❌ Never — call Paystack directly from another service
import httpx

resp = httpx.post("https://api.paystack.co/...")
```

Paystack webhook signatures must always be verified before processing:

```python
def verify_paystack_signature(payload: bytes, signature: str, secret: str) -> bool:
    import hmac, hashlib

    computed = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature)
```

---

## Environment Variables

All environment variables are validated at startup by `Settings` in `app/core/config.py`. The app refuses to start with missing or malformed config. No `os.getenv()` calls outside of `config.py`.

```python
# ✅ Always inject settings via dependency
from app.core.config import get_settings, Settings


def some_function(settings: Annotated[Settings, Depends(get_settings)]) -> ...: ...


# ❌ Never call os.getenv() inline anywhere else
import os

key = os.getenv("PAYSTACK_SECRET_KEY")
```

---

## Testing

Tests are not optional. Every service method must have a test. No PR merges without tests for new functionality.

### Stack

- **pytest** + **pytest-asyncio** for all tests
- **httpx.AsyncClient** for API-level tests
- **factory-boy** for test fixtures
- Co-located test files: `app/modules/<feature>/test_service.py` for unit tests
- `tests/` directory for integration and E2E tests

### Coverage requirements

```zsh
Statements:  80% minimum
Functions:   90% minimum
```

Run: `uv run pytest`
Coverage: `uv run pytest --cov`

### Test structure

```python
# app/modules/bookings/test_service.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.modules.bookings.service import BookingService
from app.core.exceptions import BookingNotFoundError


@pytest.mark.asyncio
async def test_get_booking_raises_when_not_found():
    db = AsyncMock()
    db.get.return_value = None
    service = BookingService(db)

    with pytest.raises(BookingNotFoundError):
        await service.get_booking("nonexistent-id")
```

---

## Git Conventions

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) enforced by `pre-commit`.

| Type | When to use |
| ------ | ------------- |
| `feat` | New endpoint, service method, or engine logic |
| `fix` | Bug fix |
| `refactor` | Restructure without behaviour change |
| `test` | Add or update tests |
| `docs` | Comments, README, AGENTS.md |
| `chore` | Dependencies, config, tooling |
| `perf` | Performance improvement |

Scope should match the module name:

```bash
feat(auth): implement OTP verification endpoint
fix(matching): handle zero-candidate edge case in rank_helpers
test(pricing): add peak hour multiplier test cases
chore(deps): bump fastapi to 0.115.2
```

---

## Commands Reference

```bash
# Install dependencies
uv sync

# Run development server (with hot reload)
uv run uvicorn app.main:app --reload --port 8000

# Run production server
uv run gunicorn -c gunicorn.conf.py app.main:app

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov

# Type check
uv run mypy app/

# Lint + format
uv run ruff check app/ --fix
uv run ruff format app/

# Database migrations
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
uv run alembic downgrade -1

# Install git hooks (run once after cloning)
uv run pre-commit install --hook-type commit-msg --hook-type pre-commit
```

---

## Opencode Agents

Project-specific agents live in `.opencode/` and are part of this repo. Config is
loaded at startup and is **not** hot-reloaded — quit and restart opencode after
adding or editing any file there.

### How to summon them

There are three ways, in descending order of reliability:

| Method | How | Notes |
| --- | --- | --- |
| **Slash command** | `/review`, `/migration-audit`, `/write-tests` | Most reliable. Pass a scope: `/review app/modules/payments` |
| **Name the agent** | "use the code-reviewer agent to check my diff" | Explicit, works from any prompt |
| **Automatic delegation** | Just ask for the work | The primary agent may delegate on its own, based on each agent's `description`. Not guaranteed — prefer one of the above when it matters |

Commands accept free-text scope after the name. With no argument they default to
the uncommitted working-tree changes.

### The agents

| Agent | Edits? | Use it for |
| --- | --- | --- |
| `code-reviewer` | No — `edit: deny` | Before committing. Reports bugs, security holes, and convention violations, ordered Blocking / Should fix / Nit. |
| `migration-auditor` | No — `edit: deny` | After changing any `model.py`. Catches drift, multiple heads, missing tables, and unsafe operations. Will not run `alembic upgrade`/`downgrade`/`revision`. |
| `test-writer` | Yes — writes tests only | After adding or changing a `service.py` method. Enforces the test rules in [Testing](#testing). Must not edit production code to make a test pass. |

### When to reach for each

- **Wrote or changed service code** → `/review`. Before commit, not after.
- **Added or edited a `model.py`** → `/migration-audit`, then `/review`. The
  auditor must run before `alembic revision --autogenerate`, because it catches
  models that autogenerate cannot see.
- **Added a public service method** → `/write-tests`. A method with no test is
  unfinished, per [Testing](#testing).
- **About to run `alembic upgrade`** → `/migration-audit` first, especially when
  the migration adds a Postgres enum.
- **Debugging a runtime error in the ORM** → the two most common causes are a
  `model.py` not imported in `alembic/env.py`, and a `relationship()` target
  that is only imported under `TYPE_CHECKING`. Both are invisible to static
  search and both surface as confusing mapper errors at runtime.

### Notes

- The agents read [AGENTS.md](AGENTS.md) as their baseline, so you do not need
  to restate conventions when invoking them.
- `code-reviewer` and `migration-auditor` are read-only. They report; you decide
  whether to apply the fix. Do not ask them to "just fix it" — that contradicts
  their permission config.
- Neither agent replaces `uv run pytest`, `uv run mypy`, or `uv run ruff check`.
  They run those to verify, but the gates still gate.

---

## What Not To Do

- Do not use `print()` — use `structlog`
- Do not use `Any` — use concrete types or TypeGuard narrowing
- Do not use `os.getenv()` outside `config.py`
- Do not put business logic in routers
- Do not call Paystack, Firebase, or Google Maps directly from service files that don't own that integration
- Do not skip Alembic and modify the DB schema directly
- Do not write a service method without a corresponding test
- Do not commit `.env` files — only `.env.example`
- Do not use sync SQLAlchemy — everything is async
- Do not add `# type: ignore` without a comment explaining why
