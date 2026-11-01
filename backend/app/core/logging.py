"""Structured logging configuration using structlog.

Uses JSON output in production and coloured console output in development.
Never use print() or the stdlib logging module directly — always use structlog.

Usage:
    import structlog
    logger = structlog.get_logger(__name__)
    logger.info("event_name", key="value", other_key=123)
"""

import logging
import sys

import structlog
from app.core.config import get_settings


def configure_logging() -> None:
    """Configure structlog. Called once at application startup."""
    settings = get_settings()
    log_level = logging.DEBUG if settings.DEBUG else logging.INFO

    # NOTE: stdlib processors (add_log_level, add_logger_name) require a stdlib
    # logging.Logger, so the logger factory below must be structlog.stdlib.LoggerFactory.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        # JSON output for log aggregation (Datadog, CloudWatch, etc.)
        formatter_processors: list[structlog.types.Processor] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable coloured output for local development
        formatter_processors = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(
                colors=True,
                pad_level=False,
                pad_event_to=0,
            ),
        ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route third-party stdlib logs (uvicorn, gunicorn, asyncpg, sqlalchemy) through the
    # same renderer so all output shares one format.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=formatter_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Let uvicorn & sqlalchemy propagate to the root handler instead of their own default ones.
    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "sqlalchemy.engine",
        "sqlalchemy.engine.Engine",
    ):
        extra_logger = logging.getLogger(name)
        extra_logger.handlers.clear()
        extra_logger.propagate = True
