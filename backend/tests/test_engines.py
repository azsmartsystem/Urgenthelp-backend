"""Unit tests for pure AI engines."""

from datetime import datetime

from app.engines.fraud import FraudContext, run_fraud_check
from app.engines.matching import HelperCandidate, JobRequest, rank_helpers, score_helper
from app.engines.pricing import PricingRequest, recommend_price
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
