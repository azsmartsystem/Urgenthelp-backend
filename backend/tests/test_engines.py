"""Unit tests for pure AI engines."""

from datetime import datetime

import pytest
from app.engines.fraud import FraudContext, run_fraud_check
from app.engines.matching import HelperCandidate, JobRequest, rank_helpers, score_helper
from app.engines.pricing import BASE_PRICES, PEAK_HOUR_MULTIPLIER, PricingRequest, recommend_price
from app.engines.trust import TrustInput, calculate_trust_score


def test_matching_engine_ranks_closest_and_highest_rated() -> None:
    candidates = [
        HelperCandidate(
            helper_id="h1",
            latitude=6.465,
            longitude=3.406,
            avg_rating=4.9,
            trust_score=95.0,
            skills=["cleaning"],
            avg_response_minutes=5,
        ),
        HelperCandidate(
            helper_id="h2",
            latitude=6.600,
            longitude=3.500,
            avg_rating=3.0,
            trust_score=50.0,
            skills=["cleaning"],
            avg_response_minutes=30,
        ),
    ]
    job = JobRequest(latitude=6.463, longitude=3.405, category="cleaning")
    ranked = rank_helpers(candidates, job)

    assert len(ranked) == 2
    assert ranked[0].helper_id == "h1"
    assert ranked[0].score > ranked[1].score


def test_matching_engine_score_helper() -> None:
    candidate = HelperCandidate(
        helper_id="h1",
        latitude=6.465,
        longitude=3.406,
        avg_rating=5.0,
        trust_score=100.0,
        skills=["cleaning"],
        avg_response_minutes=1,
    )
    job = JobRequest(latitude=6.465, longitude=3.406, category="cleaning")
    score = score_helper(candidate, job)
    assert 0.0 <= score <= 1.0
    assert score > 0.9


def test_matching_engine_respects_helper_service_radius_km() -> None:
    # ~10 km away from job
    candidate = HelperCandidate(
        helper_id="h1",
        latitude=6.555,
        longitude=3.406,
        avg_rating=5.0,
        trust_score=100.0,
        skills=["cleaning"],
        avg_response_minutes=5.0,
        service_radius_km=5.0,
    )
    job = JobRequest(latitude=6.465, longitude=3.406, category="cleaning")
    assert score_helper(candidate, job) == 0.0
    assert rank_helpers([candidate], job) == []


def test_pricing_engine_respects_base_price_override() -> None:
    req = PricingRequest(
        category="custom_db_slug",
        distance_km=0.0,
        urgency="standard",
        requested_at=datetime(2026, 10, 5, 12, 0),
        duration_hours=2.0,
        base_price_override=12_500.0,
    )
    price = recommend_price(req)
    assert price.confidence == "high"
    assert price.recommended == 25_000.0
    assert price.minimum == round(25_000.0 * 0.85, 2)
    assert price.maximum == round(25_000.0 * 1.20, 2)


def test_pricing_engine_urgent_and_standard() -> None:
    standard_req = PricingRequest(
        category="cleaning",
        distance_km=2.0,
        urgency="standard",
        requested_at=datetime(2026, 10, 5, 12, 0),  # non-peak
    )
    standard_price = recommend_price(standard_req)

    urgent_req = PricingRequest(
        category="cleaning",
        distance_km=2.0,
        urgency="urgent",
        requested_at=datetime(2026, 10, 5, 12, 0),
    )
    urgent_price = recommend_price(urgent_req)

    assert urgent_price.recommended > standard_price.recommended
    assert urgent_price.minimum < urgent_price.recommended < urgent_price.maximum
    assert urgent_price.currency == "NGN"


@pytest.mark.parametrize(
    ("slug", "expected_base"),
    [
        ("cleaning", 5_000.0),
        ("laundry", 3_000.0),
        ("cooking", 4_000.0),
        ("babysitting", 4_000.0),
        ("elder_care", 6_000.0),
        ("office_cleaning", 8_000.0),
        ("office_assistance", 5_000.0),
        ("plumbing", 8_000.0),
        ("electrical_work", 8_000.0),
        ("electrical", 8_000.0),
        ("carpentry", 7_000.0),
        ("painting", 6_000.0),
        ("generator_repair", 7_000.0),
        ("ac_repair", 9_000.0),
        ("appliance_repair", 7_000.0),
        ("gardening", 4_000.0),
        ("driving", 3_500.0),
        ("dispatch_services", 3_000.0),
        ("home_tutoring", 5_000.0),
        ("pet_care", 4_000.0),
    ],
)
def test_pricing_engine_seeded_slugs_have_high_confidence_and_exact_base_price(
    slug: str,
    expected_base: float,
) -> None:
    req = PricingRequest(
        category=slug,
        distance_km=0.0,
        urgency="standard",
        requested_at=datetime(2026, 10, 5, 12, 0),  # Monday noon (non-peak)
        duration_hours=1.0,
    )
    price = recommend_price(req)

    assert BASE_PRICES[slug] == expected_base
    assert price.confidence == "high"
    assert price.recommended == expected_base
    assert price.currency == "NGN"


def test_pricing_engine_unknown_slug_falls_back_with_low_confidence() -> None:
    req = PricingRequest(
        category="unknown_custom_category",
        distance_km=0.0,
        urgency="standard",
        requested_at=datetime(2026, 10, 5, 12, 0),
        duration_hours=1.0,
    )
    price = recommend_price(req)

    assert price.confidence == "low"
    assert price.recommended == 5_000.0


def test_pricing_engine_applies_peak_hour_multiplier_on_weekday_rush() -> None:
    off_peak = recommend_price(
        PricingRequest(
            category="cleaning",
            distance_km=0.0,
            urgency="standard",
            requested_at=datetime(2026, 10, 5, 12, 0),  # Monday 12:00
        )
    )
    morning_peak = recommend_price(
        PricingRequest(
            category="cleaning",
            distance_km=0.0,
            urgency="standard",
            requested_at=datetime(2026, 10, 5, 8, 0),  # Monday 08:00
        )
    )

    assert morning_peak.recommended == round(off_peak.recommended * PEAK_HOUR_MULTIPLIER, 2)


def test_trust_score_calculation() -> None:
    verified_helper = TrustInput(
        avg_rating=5.0,
        completion_rate=1.0,
        is_id_verified=True,
        open_disputes=0,
        cancellation_rate=0.0,
    )
    score = calculate_trust_score(verified_helper)
    assert score == 95.0

    unverified_helper = TrustInput(
        avg_rating=3.0,
        completion_rate=0.5,
        is_id_verified=False,
        open_disputes=2,
        cancellation_rate=0.4,
    )
    low_score = calculate_trust_score(unverified_helper)
    assert low_score < score


def test_fraud_check_triggers_flags() -> None:
    suspicious_context = FraudContext(
        account_age_hours=1.0,
        payment_failure_rate=0.7,
        cancellation_rate=0.5,
        booking_speed_seconds=20.0,
        gps_speed_kph=150.0,
        duplicate_phone_count=2,
        open_disputes=3,
    )
    flags = run_fraud_check(suspicious_context)
    assert len(flags) > 0
    flag_names = [f.name for f in flags]
    assert "cancellation_rate_above_40pct" in flag_names
    assert "payment_failure_rate_above_50pct" in flag_names
