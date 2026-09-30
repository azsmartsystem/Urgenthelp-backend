"""Fraud detection engine — rule-based flag system.

Each rule is a named predicate. When triggered, an action is taken and
the flag is recorded for admin review. This replaces the need for an ML
fraud model at MVP scale.
"""

from dataclasses import dataclass
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

FraudAction = Literal["flag_for_review", "suspend", "require_manual_verify", "discard"]


@dataclass(frozen=True)
class FraudFlag:
    name: str
    action: FraudAction
    severity: Literal["critical", "high", "medium", "low"]


@dataclass
class FraudContext:
    """Inputs required to evaluate fraud rules for a user/event."""

    account_age_hours: float
    payment_failure_rate: float  # 0.0 - 1.0
    cancellation_rate: float  # 0.0 - 1.0
    booking_speed_seconds: float  # time between booking + review submission
    gps_speed_kph: float  # detected speed during job (impossible if very high)
    duplicate_phone_count: int  # accounts sharing this phone
    open_disputes: int


RULES: list[tuple[str, FraudAction, Literal["critical", "high", "medium", "low"]]] = [
    ("multiple_accounts_same_phone", "suspend", "critical"),
    ("payment_failure_rate_above_50pct", "flag_for_review", "high"),
    ("new_account_first_booking", "require_manual_verify", "medium"),
    ("gps_speed_exceeds_200kph", "flag_for_review", "high"),
    ("review_submitted_under_60s", "discard", "low"),
    ("cancellation_rate_above_40pct", "flag_for_review", "medium"),
]


def _evaluate(rule_name: str, ctx: FraudContext) -> bool:
    match rule_name:
        case "multiple_accounts_same_phone":
            return ctx.duplicate_phone_count > 1
        case "payment_failure_rate_above_50pct":
            return ctx.payment_failure_rate > 0.50
        case "new_account_first_booking":
            return ctx.account_age_hours < 24
        case "gps_speed_exceeds_200kph":
            return ctx.gps_speed_kph > 200
        case "review_submitted_under_60s":
            return ctx.booking_speed_seconds < 60
        case "cancellation_rate_above_40pct":
            return ctx.cancellation_rate > 0.40
        case _:
            return False


def run_fraud_check(ctx: FraudContext) -> list[FraudFlag]:
    """Evaluate all rules and return triggered flags."""
    triggered: list[FraudFlag] = []
    for name, action, severity in RULES:
        if _evaluate(name, ctx):
            triggered.append(FraudFlag(name=name, action=action, severity=severity))
            logger.warning("fraud_flag_triggered", rule=name, action=action, severity=severity)
    return triggered
