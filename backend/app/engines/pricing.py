"""AI Pricing Engine — rule-based price recommendation.

Returns a {min, recommended, max} price range with a confidence label.
This is a formula, not ML. Admin-configurable base prices and multipliers.
Replace the formula with a regression model later without touching the interface.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PricingRequest:
    category: str
    distance_km: float
    urgency: Literal["standard", "urgent"]
    requested_at: datetime
    duration_hours: float = 1.0


@dataclass(frozen=True)
class PriceRange:
    minimum: float
    recommended: float
    maximum: float
    currency: str
    confidence: Literal["high", "medium", "low"]


# ── Base prices per category (NGN) — configurable via admin panel in future
BASE_PRICES: dict[str, float] = {
    "cleaning":          5_000,
    "laundry":           3_000,
    "cooking":           4_000,
    "babysitting":       4_000,
    "elder_care":        6_000,
    "plumbing":          8_000,
    "electrical":        8_000,
    "carpentry":         7_000,
    "painting":          6_000,
    "generator_repair":  7_000,
    "ac_repair":         9_000,
    "appliance_repair":  7_000,
    "gardening":         4_000,
    "driving":           3_500,
    "home_tutoring":     5_000,
    "pet_care":          4_000,
}

RATE_PER_KM: float = 150.0       # NGN per km travel
URGENCY_MULTIPLIER: float = 1.30  # 30% premium for urgent requests
PEAK_HOUR_MULTIPLIER: float = 1.20


def _is_peak_hour(dt: datetime) -> bool:
    """Morning rush (7–9am) or evening rush (5–8pm) on weekdays."""
    weekday = dt.weekday()  # 0=Monday, 6=Sunday
    hour = dt.hour
    is_weekday = weekday < 5
    return is_weekday and (7 <= hour <= 9 or 17 <= hour <= 20)


def recommend_price(request: PricingRequest) -> PriceRange:
    """Compute a recommended price range for a booking request."""
    base = BASE_PRICES.get(request.category.lower(), 5_000)
    travel = request.distance_km * RATE_PER_KM
    duration_factor = request.duration_hours  # linear scaling

    raw = (base + travel) * duration_factor

    if request.urgency == "urgent":
        raw *= URGENCY_MULTIPLIER

    if _is_peak_hour(request.requested_at):
        raw *= PEAK_HOUR_MULTIPLIER

    known_category = request.category.lower() in BASE_PRICES
    confidence: Literal["high", "medium", "low"] = "high" if known_category else "low"

    logger.debug(
        "pricing_engine_computed",
        category=request.category,
        base=base,
        travel=travel,
        recommended=raw,
        urgency=request.urgency,
    )

    return PriceRange(
        minimum=round(raw * 0.85, 2),
        recommended=round(raw, 2),
        maximum=round(raw * 1.20, 2),
        currency="NGN",
        confidence=confidence,
    )
