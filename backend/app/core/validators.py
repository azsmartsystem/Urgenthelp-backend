"""Core validation utilities."""

import re
from typing import Annotated, NewType

from pydantic import BeforeValidator

# A phone number that is guaranteed to be in canonical form: 13 bare digits
# starting 234, with no '+', spaces, or dashes. This is the only form that may
# reach storage, a Redis key, or a database comparison.
#
# It is a NewType, so it costs nothing at runtime (it is erased to str) but
# gives mypy a real guarantee: a service method annotated `CanonicalPhone`
# cannot be handed "0815… " by a future caller, which would otherwise write a
# Redis OTP key that verify_otp can never find.
#
# Note for future SMS/email dispatch code: this is deliberately NOT E.164. Every
# SMS gateway (Twilio, Termii, Infobip) requires the '+234…' form. Re-add the
# plus at the gateway boundary in one named helper — do not let a '+' into
# storage, or format drift returns.
CanonicalPhone = NewType("CanonicalPhone", str)


def normalize_nigerian_phone(v: object) -> str:
    """Normalize any Nigerian phone number to canonical 234XXXXXXXXXX format.

    Supported input formats:
    - 08153551975 (11-digit local format)
    - 2348153551975 (13-digit format without +)
    - +2348153551975 (E.164 international format)
    - Formatted strings with spaces or dashes (e.g. '0815 355 1975', '+234-815-355-1975')

    Returns:
    - 234XXXXXXXXXX (exactly 13 digits)

    This runs as a pydantic BeforeValidator, so it is called with the raw input
    before strict type checking. A non-string must raise ValueError here —
    letting it reach .strip() raises AttributeError, which is not a validation
    error and surfaces as an unhandled 500.
    """
    if not isinstance(v, str):
        raise ValueError("Phone number must be a string.")

    # Remove all whitespace, dashes, dots, brackets
    cleaned = re.sub(r"[\s\-\(\)\.]", "", v.strip())

    # Remove leading plus sign if present
    has_plus = cleaned.startswith("+")
    if has_plus:
        cleaned = cleaned[1:]

    # Convert 11-digit local format (starts with 0, e.g. 08153551975) to 2348153551975.
    # Only bare local numbers convert: "+0801…" is not a valid international format,
    # so it must fall through and be rejected rather than silently normalised.
    if cleaned.startswith("0") and len(cleaned) == 11 and not has_plus:
        cleaned = "234" + cleaned[1:]

    # Validate against official Nigerian mobile prefix pattern:
    # Starts with 234, followed by 7, 8, or 9, followed by 9 digits (total 13 digits).
    #
    # Use an explicit ASCII class, not \d. In Python 3, \d matches any Unicode
    # decimal digit for str patterns, so a phone written with Arabic-Indic
    # digits would validate and be returned as a "canonical" value, breaking the
    # CanonicalPhone contract and writing a non-ASCII Redis OTP key. It would
    # also fail the users CHECK constraint, whose Postgres character classes are
    # ASCII-only, surfacing as an IntegrityError 500 rather than a 422.
    if not re.match(r"^234[789][0-9]{9}$", cleaned):
        raise ValueError(
            "Invalid Nigerian phone number. Must be a valid 11-digit local number "
            "(e.g. 08153551975) or 13-digit international format (e.g. 2348153551975)."
        )

    return cleaned


# The annotated type for any pydantic field that accepts a phone number. The
# annotation is deliberately `str`, not `CanonicalPhone`: a request field receives
# whatever the user typed ("0815 355 1975") and this validator returns the
# canonical form. Typing the field as CanonicalPhone would mean claiming the raw
# input is already canonical, which is false at every constructor call site.
#
# CanonicalPhone is applied at the *service* boundary instead, where the value is
# known to be normalized — see AuthService.send_otp and AuthService.verify_otp.
#
# This is the only sanctioned way to declare a phone field. tests/test_validators.py
# enforces that across every schemas.py, so a new endpoint cannot quietly opt out.
NigerianPhone = Annotated[str, BeforeValidator(normalize_nigerian_phone)]
