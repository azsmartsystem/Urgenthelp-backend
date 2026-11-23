"""AI Matching Engine — rule-based weighted scoring.

Ranks available helpers for a given job request.
This module has a clean interface so the scoring logic can be replaced
with an ML model later without changing any calling code.
"""

from dataclasses import dataclass

import structlog
from app.core.geo import haversine_km

logger = structlog.get_logger(__name__)

# Weight configuration — these should eventually be admin-tunable via DB
WEIGHTS = {
    "distance": 0.40,
    "rating": 0.25,
    "skill": 0.20,
    "trust": 0.10,
    "speed": 0.05,  # average response speed to past jobs
}

MAX_DISTANCE_KM = 20.0


@dataclass(frozen=True)
class HelperCandidate:
    """Input data for scoring a single helper."""

    helper_id: str
    latitude: float
    longitude: float
    avg_rating: float  # 0.0 - 5.0
    trust_score: float  # 0.0 - 100.0
    skills: list[str]
    avg_response_minutes: float
    service_radius_km: float = MAX_DISTANCE_KM


@dataclass(frozen=True)
class JobRequest:
    """Inputs describing the job to match against."""

    latitude: float
    longitude: float
    category: str


@dataclass(frozen=True)
class RankedHelper:
    helper_id: str
    score: float  # 0.0 - 1.0


_haversine_km = haversine_km


def _distance_score(distance_km: float, max_distance_km: float = MAX_DISTANCE_KM) -> float:
    """Linear decay from 1.0 at 0 km to 0.0 at max_distance_km."""
    if max_distance_km <= 0.0:
        return 0.0
    return max(0.0, 1.0 - distance_km / max_distance_km)


def _rating_score(avg_rating: float) -> float:
    return avg_rating / 5.0


def _skill_score(skills: list[str], category: str) -> float:
    return 1.0 if category.lower() in [s.lower() for s in skills] else 0.0


def _trust_score_normalised(trust_score: float) -> float:
    return trust_score / 100.0


def _speed_score(avg_response_minutes: float) -> float:
    """Lower response time = higher score. Cap at 60 minutes."""
    return max(0.0, 1.0 - avg_response_minutes / 60.0)


def score_helper(candidate: HelperCandidate, job: JobRequest) -> float:
    """Compute a composite match score for one helper. Returns 0.0 - 1.0."""
    distance_km = _haversine_km(
        job.latitude,
        job.longitude,
        candidate.latitude,
        candidate.longitude,
    )
    max_radius = candidate.service_radius_km
    if max_radius <= 0.0 or distance_km > max_radius:
        return 0.0  # outside service radius — exclude

    return (
        _distance_score(distance_km, max_distance_km=max_radius) * WEIGHTS["distance"]
        + _rating_score(candidate.avg_rating) * WEIGHTS["rating"]
        + _skill_score(candidate.skills, job.category) * WEIGHTS["skill"]
        + _trust_score_normalised(candidate.trust_score) * WEIGHTS["trust"]
        + _speed_score(candidate.avg_response_minutes) * WEIGHTS["speed"]
    )


def rank_helpers(
    candidates: list[HelperCandidate],
    job: JobRequest,
    top_n: int = 5,
) -> list[RankedHelper]:
    """Return the top_n helpers ranked by match score, highest first."""
    scored = [RankedHelper(helper_id=c.helper_id, score=score_helper(c, job)) for c in candidates]
    ranked = sorted(scored, key=lambda r: r.score, reverse=True)
    result = [r for r in ranked if r.score > 0.0][:top_n]

    logger.info(
        "matching_engine_ranked",
        total_candidates=len(candidates),
        eligible=len([r for r in ranked if r.score > 0]),
        top_score=result[0].score if result else None,
    )
    return result
