# ALLinHELP Backend

ALLinHELP is an AI-assisted, on-demand marketplace platform for local services. The backend provides a modular, async RESTful API and real-time infrastructure to handle user onboarding, helper dispatching, pricing, payments, in-app messaging, trust verification, and administrative controls.

---

## Architectural Overview

The application is structured as a Modular Monolith. This design enforces strict service boundaries across domains while maintaining the simplicity of single-unit deployment.

### Key Architectural Characteristics

- Framework: FastAPI (fully asynchronous ASGI)
- Database Layer: PostgreSQL accessed via SQLAlchemy 2.0 and asyncpg
- Ephemeral Data & Cache: Redis for session storage, rate limiting, and OTP handling
- Process Management: Uvicorn for local hot reload; Gunicorn with UvicornWorker for production
- Pure AI Engines: Rule-based, side-effect-free algorithms for matching, pricing, trust evaluation, and fraud monitoring, designed for straightforward migration to ML pipelines
- Type Safety: Strict static typing verified by Mypy and validated at runtime with Pydantic v2

---

## AI Engines

All decision-making engines reside in `backend/app/engines/`. They are implemented as pure functions that operate on strongly typed dataclasses without direct database or network dependencies.

1. Matching Engine (`matching.py`)
   Computes composite match scores using great-circle Haversine geospatial distance, category skills matching, helper historical ratings, platform trust scores, and response times.

2. Dynamic Pricing Engine (`pricing.py`)
   Calculates base category costs, travel distance compensation, weekday peak-hour multipliers, and urgency surcharges to output deterministic price ranges (minimum, recommended, maximum).

3. Trust and Verification Engine (`trust.py`)
   Evaluates user reliability based on average ratings, job completion ratios, identity verification status, dispute history, and cancellation frequency to generate a normalized score between 0 and 100.

4. Fraud Detection Engine (`fraud.py`)
   Evaluates risk heuristics (excessive payment failures, high cancellation spikes, abnormal GPS travel speeds, disposable account velocity) and outputs categorized risk flags with severity levels.

---

## Repository Structure

```text
urgenthelp/
├── .gitignore                   # Root ignore rules for Git
├── AGENTS.md                    # Coding standards and AI agent guidelines
├── DISCOVERY_NOTES.md           # Client architectural decisions and scoping notes
├── TRACKER.md                   # 13-week milestone, payment, and velocity tracker
└── backend/
    ├── pyproject.toml           # Project dependencies, build settings, Ruff and Mypy configurations
    ├── uv.lock                  # Pinned dependency lockfile
    ├── gunicorn.conf.py         # Multi-worker production ASGI server configuration
    ├── .pre-commit-config.yaml  # Pre-commit hook definitions (Ruff, Mypy, Conventional Commits)
    ├── .env.example             # Template of environment variables
    ├── alembic.ini              # Alembic migration configuration
    ├── alembic/
    │   └── env.py               # Async Alembic execution environment
    ├── app/
    │   ├── main.py              # Application factory, middleware, and router registration
    │   ├── core/                # Configuration, structured logging, exceptions, and security
    │   │   ├── config.py        # Pydantic Settings management
    │   │   ├── exceptions.py    # Custom domain exception hierarchy and handlers
    │   │   ├── logging.py       # Structlog JSON and console formatters
    │   │   └── security.py      # JWT authentication, hashing, and RBAC dependencies
    │   ├── db/                  # Database connectivity and ORM base classes
    │   │   ├── base.py          # Mixins (UUID primary keys, timestamps) and declarative base
    │   │   └── session.py       # Async SQLAlchemy engine and session dependency
    │   ├── engines/             # Pure algorithmic AI domains (Matching, Pricing, Trust, Fraud)
    │   ├── schemas/             # Shared Pydantic base models and pagination wrappers
    │   └── modules/             # Feature modules (Domain-driven boundaries)
    │       ├── admin/           # Administrative endpoints and audit controls
    │       ├── auth/            # Phone/password registration, OTP verification, JWT lifecycle
    │       ├── bookings/        # Booking lifecycle and state transition management
    │       ├── categories/      # Service categories and base catalog management
    │       ├── helpers/         # Service provider profiles, availability, and skills
    │       ├── messaging/       # Real-time WebSocket messaging and historical logs
    │       ├── notifications/   # Push notification dispatching via Firebase
    │       ├── payments/        # Paystack webhook verification and checkout initiation
    │       ├── reviews/         # Helper feedback, ratings, and reputation tracking
    │       ├── users/           # User profile and account management
    │       └── wallet/          # Balance tracking, transactions ledger, and withdrawals
    └── tests/
        ├── conftest.py          # Asynchronous test fixtures and test client
        ├── test_engines.py      # Unit tests for matching, pricing, trust, and fraud
        └── test_health.py       # Health check endpoint integration test
```

---

## Prerequisites

Ensure the following tools are installed on your workstation:

- Python: Version 3.12 or higher
- uv: High-performance Python package and environment manager
- PostgreSQL: Version 15 or higher
- Redis: Version 7 or higher
- Git: Version 2.30 or higher

---

## Getting Started

### 1. Clone the Repository

```bash
git clone git@github.com:azsmartsystem/Urgenthelp-backend.git
cd Urgenthelp-backend
```

### 2. Environment Configuration

Navigate to the `backend` directory and initialize your local environment file:

```bash
cd backend
cp .env.example .env
```

Edit `.env` to supply local credentials for PostgreSQL, Redis, and JWT secrets. Example:

```env
ENVIRONMENT=development
PORT=8000
DEBUG=true

DATABASE_URL=postgresql://postgres:password@localhost:5432/allinhelp_db
REDIS_URL=redis://localhost:6379/0

JWT_ACCESS_SECRET=your_32_character_minimum_access_secret_here
JWT_REFRESH_SECRET=your_32_character_minimum_refresh_secret_here

PAYSTACK_SECRET_KEY=sk_test_placeholder
PAYSTACK_PUBLIC_KEY=pk_test_placeholder

AWS_ACCESS_KEY_ID=placeholder
AWS_SECRET_ACCESS_KEY=placeholder
AWS_S3_BUCKET=allinhelp-dev

FIREBASE_CREDENTIALS_JSON=./firebase-service-account.json
GOOGLE_MAPS_API_KEY=placeholder
```

### 3. Install Dependencies

Install all project packages and development tools using `uv`:

```bash
uv sync --dev
```

This creates an isolated virtual environment in `backend/.venv`.

### 4. Apply Database Migrations

Apply the latest schema migrations to your database:

```bash
uv run alembic upgrade head
```

To create a new migration after modifying models:

```bash
uv run alembic revision --autogenerate -m "describe change"
```

### 5. Start the Development Server

Start the local server with hot reloading enabled:

```bash
uv run uvicorn app.main:app --reload --port 8000
```

The API service is accessible at `http://localhost:8000`. Interactive OpenAPI documentation is available at `http://localhost:8000/docs` in non-production environments.

---

## Production Deployment

For production deployments, execute Gunicorn with the preconfigured multi-worker ASGI setup:

```bash
uv run gunicorn -c gunicorn.conf.py app.main:app
```

The worker count is automatically computed based on available CPU cores `(2 * CPU + 1)` and binds to `0.0.0.0:$PORT`.

---

## Code Quality and Testing

The repository enforces strict linting, formatting, and type-checking rules.

### Running Automated Tests

Run the test suite with coverage reporting:

```bash
uv run pytest
```

Test coverage thresholds are configured at a minimum of 80% total coverage.

### Static Analysis and Type Checking

Run Mypy in strict mode:

```bash
uv run mypy app/
```

### Code Formatting and Linting

Check and format the codebase using Ruff:

```bash
# Check code for lint violations
uv run ruff check app/ --fix

# Format source files
uv run ruff format app/
```

---

## Git Hooks and Commit Standards

The project utilizes `pre-commit` to guarantee that formatting, linting, type checks, and commit message formats are verified prior to writing commits.

### Installing Hooks

Install the Git hooks into your `.git` directory:

```bash
cd backend
uv run pre-commit install --hook-type commit-msg --hook-type pre-commit
```

### Commit Message Conventions

Commit messages must strictly follow the Conventional Commits specification:

```text
<type>(<scope>): <short description>
```

Allowed types:
- `feat`: A new user-facing feature or endpoint
- `fix`: A bug fix
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `test`: Adding or updating test cases
- `docs`: Documentation updates
- `chore`: Tooling, dependency updates, or internal configuration
- `perf`: Performance optimizations

Examples:
- `feat(auth): add phone and otp verification endpoints`
- `fix(pricing): correct peak hour surcharge calculation`
- `test(engines): add boundary tests for trust score`

---

## License

Proprietary and confidential. Unauthorized copying or distribution is strictly prohibited.
