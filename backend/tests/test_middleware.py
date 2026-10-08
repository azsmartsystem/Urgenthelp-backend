"""Tests for the request timing middleware."""

import pytest
from app.core.middleware import RequestTimingMiddleware, format_duration
from httpx import AsyncClient
from structlog.testing import capture_logs


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (0.42, "0.4ms"),
        (3.04, "3.0ms"),
        (999.94, "999.9ms"),
        (1000.0, "1.0s"),
        (2345.6, "2.3s"),
        (59_999.0, "60.0s"),
        (60_000.0, "1m0.0s"),
        (65_432.1, "1m5.4s"),
        (600_000.0, "10m0.0s"),
    ],
)
def test_format_duration(duration_ms: float, expected: str) -> None:
    assert format_duration(duration_ms) == expected


@pytest.mark.asyncio
async def test_request_timing_logs_duration(client: AsyncClient) -> None:
    with capture_logs() as logs:
        response = await client.get("/health")

    assert response.status_code == 200
    events = [entry for entry in logs if entry["event"] == "http_request"]
    assert len(events) == 1
    event = events[0]
    assert event["method"] == "GET"
    assert event["path"] == "/health"
    assert event["status_code"] == 200
    assert event["log_level"] == "info"
    assert isinstance(event["duration_ms"], float)
    assert event["duration_ms"] >= 0
    assert event["duration"].endswith(("ms", "s"))


@pytest.mark.asyncio
async def test_request_timing_logs_client_errors_at_warning(client: AsyncClient) -> None:
    with capture_logs() as logs:
        response = await client.get("/does-not-exist")

    assert response.status_code == 404
    events = [entry for entry in logs if entry["event"] == "http_request"]
    assert len(events) == 1
    assert events[0]["status_code"] == 404
    assert events[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    sent: list[str] = []

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        sent.append(str(scope["type"]))

    middleware = RequestTimingMiddleware(app)  # type: ignore[arg-type]

    await middleware({"type": "websocket"}, receive=None, send=None)  # type: ignore[arg-type]

    assert sent == ["websocket"]
