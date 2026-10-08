"""ASGI middleware for per-request timing.

Emits one structured `http_request` log event per HTTP request containing the
end-to-end duration, rendered in the shortest sensible unit (3.0ms, 2.0s, 1m5.0s)
alongside the raw millisecond value for aggregation.
"""

import time
from typing import TypedDict

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)


class HttpRequestLog(TypedDict):
    """Structured log event for HTTP requests."""

    method: str
    path: str
    status_code: int
    duration: str
    duration_ms: float
    client: str | None


def format_duration(duration_ms: float) -> str:
    """Render a duration as ms below one second, s below one minute, else m+s."""
    if duration_ms < 1000:
        return f"{duration_ms:.1f}ms"
    total_seconds = duration_ms / 1000
    if total_seconds < 60:
        return f"{total_seconds:.1f}s"
    minutes, seconds = divmod(total_seconds, 60)
    return f"{int(minutes)}m{seconds:.1f}s"


class RequestTimingMiddleware:
    """Measure request duration and log it as a structured event."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_with_timing(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, send_with_timing)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            client = scope.get("client")
            event: HttpRequestLog = {
                "method": str(scope.get("method", "")),
                "path": str(scope.get("path", "")),
                "status_code": status_code,
                "duration": format_duration(duration_ms),
                "duration_ms": round(duration_ms, 2),
                "client": str(client[0]) if client else None,
            }
            if status_code >= 500:
                logger.error("http_request", **event)
            elif status_code >= 400:
                logger.warning("http_request", **event)
            else:
                logger.info("http_request", **event)
