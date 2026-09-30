"""Trust score engine — deterministic formula updated after each booking."""

from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TrustInput:
    avg_rating: float           # 0.0 – 5.0
    completion_rate: float      # 0.0 – 1.0  (completed / total accepted)
    is_id_verified: bool
    open_disputes: int
    cancellation_rate: float    # 0.0 – 1.0


def calculate_trust_score(data: TrustInput) -> float:
    """Return a trust score between 0.0 and 100.0.

    Component breakdown:
        Rating component     — max 50 pts
        Completion component — max 30 pts
        Verification bonus   — 15 pts
        Dispute penalty      — -5 pts each
        Cancellation penalty — up to -10 pts
    """
    rating_pts      = (data.avg_rating / 5.0) * 50
    completion_pts  = data.completion_rate * 30
    verification_pt = 15.0 if data.is_id_verified else 0.0
    dispute_pen     = data.open_disputes * -5.0
    cancel_pen      = data.cancellation_rate * -10.0

    raw = rating_pts + completion_pts + verification_pt + dispute_pen + cancel_pen

    score = max(0.0, min(100.0, raw))  # clamp to [0, 100]

    logger.debug("trust_score_calculated", score=score, input=data)
    return round(score, 2)
