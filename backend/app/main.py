"""ALLinHELP FastAPI application entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import close_db, init_db

# --- Module routers (import as each module is built) ---
from app.modules.auth.router import router as auth_router
from app.modules.users.router import router as users_router
from app.modules.helpers.router import router as helpers_router
from app.modules.bookings.router import router as bookings_router
from app.modules.categories.router import router as categories_router
from app.modules.payments.router import router as payments_router
from app.modules.wallet.router import router as wallet_router
from app.modules.messaging.router import router as messaging_router
from app.modules.reviews.router import router as reviews_router
from app.modules.notifications.router import router as notifications_router
from app.modules.admin.router import router as admin_router

settings = get_settings()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    configure_logging()
    logger.info("allinhelp_api_starting", environment=settings.ENVIRONMENT)
    await init_db()
    yield
    await close_db()
    logger.info("allinhelp_api_shutdown")


def create_application() -> FastAPI:
    """Application factory — creates and configures the FastAPI app."""
    app = FastAPI(
        title="ALLinHELP API",
        description="Marketplace backend for ALLinHELP service platform",
        version="0.1.0",
        # Disable docs in production
        docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
        redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
        openapi_url="/openapi.json" if settings.ENVIRONMENT != "production" else None,
        lifespan=lifespan,
    )

    # --- Middleware (order matters: outermost = last added) ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOSTS,
    )

    # --- Exception handlers ---
    register_exception_handlers(app)

    # --- API Routers ---
    API_PREFIX = "/api/v1"
    app.include_router(auth_router,          prefix=f"{API_PREFIX}/auth",          tags=["Auth"])
    app.include_router(users_router,         prefix=f"{API_PREFIX}/users",         tags=["Users"])
    app.include_router(helpers_router,       prefix=f"{API_PREFIX}/helpers",       tags=["Helpers"])
    app.include_router(bookings_router,      prefix=f"{API_PREFIX}/bookings",      tags=["Bookings"])
    app.include_router(categories_router,    prefix=f"{API_PREFIX}/categories",    tags=["Categories"])
    app.include_router(payments_router,      prefix=f"{API_PREFIX}/payments",      tags=["Payments"])
    app.include_router(wallet_router,        prefix=f"{API_PREFIX}/wallet",        tags=["Wallet"])
    app.include_router(messaging_router,     prefix=f"{API_PREFIX}/messages",      tags=["Messaging"])
    app.include_router(reviews_router,       prefix=f"{API_PREFIX}/reviews",       tags=["Reviews"])
    app.include_router(notifications_router, prefix=f"{API_PREFIX}/notifications", tags=["Notifications"])
    app.include_router(admin_router,         prefix=f"{API_PREFIX}/admin",         tags=["Admin"])

    @app.get("/health", tags=["Health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    return app


app = create_application()
