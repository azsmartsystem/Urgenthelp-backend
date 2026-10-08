"""Unit tests for app/core/validators.py and the phone-format invariants it owns.

These live here rather than in app/modules/auth/test_service.py because they
exercise app/core, not AuthService — but they also cover the schema-level
normalisation applied to auth requests, so both layers are covered.
"""

import re
from typing import cast

import pytest
from app.core.validators import CanonicalPhone, normalize_nigerian_phone
from app.modules.auth.schemas import (
    LoginRequest,
    OTPVerifyRequest,
    RegisterRequest,
    SendOTPRequest,
)
from app.modules.users.model import User
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import CheckConstraint, Table

# The regex embedded in the users table CheckConstraint, extracted from the model
# so this test exercises the real constraint rather than a copy of it.
_USERS_TABLE = cast(Table, User.__table__)
_PHONE_CONSTRAINT_SQL = next(
    str(c.sqltext)
    for c in _USERS_TABLE.constraints
    if isinstance(c, CheckConstraint) and c.name == "ck_users_phone_format"
)
_PHONE_CONSTRAINT_RE = re.search(r"'(\^.*\$)'", _PHONE_CONSTRAINT_SQL)
assert _PHONE_CONSTRAINT_RE is not None, f"could not parse constraint: {_PHONE_CONSTRAINT_SQL}"
CONSTRAINT_PATTERN = re.compile(_PHONE_CONSTRAINT_RE.group(1))

VALID_INPUTS = [
    ("08153551975", "2348153551975"),
    ("2348153551975", "2348153551975"),
    ("+2348153551975", "2348153551975"),
    ("0815 355 1975", "2348153551975"),
    ("+234 815 355 1975", "2348153551975"),
    ("+234-815-355-1975", "2348153551975"),
    ("07031234567", "2347031234567"),
    ("09081234567", "2349081234567"),
    ("09121234567", "2349121234567"),
]

INVALID_INPUTS = [
    "0815355197",  # 10 digits (too short)
    "081535519755",  # 12 digits (too long)
    "06153551975",  # invalid starting digit (06)
    "+1234567890123",  # US number
    "2345012345678",  # invalid prefix
    "abc8153551975",  # alphanumeric
    "",  # empty
    "+08153551975",  # '+' on a local number is not an international format
    2348153551975,  # non-string must be a validation error, not an AttributeError
    None,
]


@pytest.mark.parametrize(("raw_input", "expected"), VALID_INPUTS)
def test_normalize_nigerian_phone_valid(raw_input: str, expected: str) -> None:
    assert normalize_nigerian_phone(raw_input) == expected


@pytest.mark.parametrize("invalid_input", INVALID_INPUTS)
def test_normalize_nigerian_phone_invalid_raises(invalid_input: object) -> None:
    with pytest.raises(ValueError):
        normalize_nigerian_phone(invalid_input)


@pytest.mark.parametrize(("raw_input", "expected"), VALID_INPUTS)
def test_db_constraint_accepts_every_valid_phone(raw_input: str, expected: str) -> None:
    """Every phone the app accepts must satisfy the users table CHECK constraint."""
    assert CONSTRAINT_PATTERN.match(normalize_nigerian_phone(raw_input)) is not None


@pytest.mark.parametrize("invalid_input", INVALID_INPUTS)
def test_db_constraint_rejects_every_invalid_phone(invalid_input: object) -> None:
    """The constraint must not admit anything the validator refuses."""
    if not isinstance(invalid_input, str):
        pytest.skip("non-string input is rejected before reaching the database")
    try:
        normalized = normalize_nigerian_phone(invalid_input)
    except ValueError:
        return
    assert CONSTRAINT_PATTERN.match(normalized) is None


@pytest.mark.parametrize(
    "phone",
    [
        "2341011112222",  # prefix 1 — not a Nigerian mobile prefix
        "2340011112222",  # prefix 0
        "2346111112222",  # prefix 6
        "2345011112222",  # prefix 5
        "12348011112222",  # missing 234 country code
        "234815355197",  # 12 digits
        "23481535519755",  # 14 digits
        "23481535519a",  # non-digit
    ],
)
def test_db_constraint_rejects_malformed_phone_forms(phone: str) -> None:
    """Tripwire against the constraint being loosened out of step with the app.

    The two behavioural tests above cannot catch this direction of drift: the
    validator is the stricter of the pair, so a widened constraint still agrees
    with everything it is compared against. These cases assert the constraint
    rejects forms the app would never produce, which only holds if its regex is
    still pinned to the 234 + [789] + 9 digits shape. Alembic does not diff CHECK
    constraints, so nothing else in the toolchain would notice.
    """
    assert CONSTRAINT_PATTERN.match(phone) is None


def test_register_schema_normalizes_local_phone() -> None:
    req = RegisterRequest(
        phone="08153551975",
        full_name="Test User",
        password="SecurePassword1!",
        role="customer",
    )
    assert req.phone == "2348153551975"


def test_login_schema_normalizes_local_phone() -> None:
    req = LoginRequest(phone="08153551975", password="SecurePassword1!")
    assert req.phone == "2348153551975"


def test_send_otp_schema_normalizes_local_phone() -> None:
    req = SendOTPRequest(phone="0815 355 1975")
    assert req.phone == "2348153551975"


def test_otp_verify_schema_normalizes_local_phone() -> None:
    req = OTPVerifyRequest(phone="+234-815-355-1975", otp="123456")
    assert req.phone == "2348153551975"


def test_every_phone_field_uses_the_normalizing_type() -> None:
    """Tripwire: no schema may declare a phone field that skips normalization.

    Phone normalization is opt-in by nature — a new endpoint that writes
    `phone: str` would silently accept and store whatever the user typed,
    breaking cross-format login and Redis OTP lookups with nothing to catch it.
    This scans every schemas.py in the project and fails if any `phone` field is
    not annotated with NigerianPhone.
    """
    import ast
    import pathlib

    allowed = {"NigerianPhone", "CanonicalPhone"}
    offenders: list[str] = []
    schemas_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "modules"

    for path in sorted(schemas_dir.rglob("schemas.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                target = stmt.target
                if not (isinstance(target, ast.Name) and target.id == "phone"):
                    continue
                annotation = ast.unparse(stmt.annotation)
                if annotation not in allowed:
                    where = f"{path.relative_to(path.parents[2])}:{node.name}.phone"
                    offenders.append(f"{where}: {annotation}")

    assert not offenders, "phone fields must use NigerianPhone: " + "; ".join(offenders)


# ─── Idempotence ─────────────────────────────────────────────────────────────
#
# verify_otp does not normalize from scratch — it narrows an already-normalized
# schema value with CanonicalPhone. That cast is only honest if normalization is
# a fixed point, i.e. normalizing an already-canonical number is a no-op. If it
# ever stops being one, the key verify_otp reads can drift from the key
# send_otp wrote, and the phone stored on the User can drift from both.


@pytest.mark.parametrize(("raw_input", "expected"), VALID_INPUTS)
def test_normalization_is_idempotent(raw_input: str, expected: str) -> None:
    once = normalize_nigerian_phone(raw_input)
    assert once == expected
    assert normalize_nigerian_phone(once) == once


@pytest.mark.parametrize(("raw_input", "expected"), VALID_INPUTS)
def test_canonical_phone_narrowing_preserves_the_value(raw_input: str, expected: str) -> None:
    """The NewType cast used at the service boundary must be value-preserving."""
    canonical = CanonicalPhone(normalize_nigerian_phone(raw_input))
    assert canonical == expected
    assert len(canonical) == 13
    assert canonical.isascii() and canonical.isdigit()


# ─── KNOWN DEFECT: non-ASCII digits are accepted ─────────────────────────────
#
# normalize_nigerian_phone validates with `\d`, which is Unicode-aware for str
# patterns in Python 3. So a string carrying Arabic-Indic / Devanagari / Bengali
# digits satisfies the pattern and is returned as "canonical".
#
# Consequences, all of them real:
#   1. It breaks CanonicalPhone's documented contract ("13 bare digits").
#   2. The value fails the users CHECK constraint `^234[789][0-9]{9}$`, because
#      Postgres character classes are ASCII-only. The insert therefore fails
#      with an IntegrityError — a 500, on an unauthenticated endpoint, instead
#      of a 422.
#   3. The OTP Redis key becomes `otp:2348٨…`, reintroducing exactly the format
#      drift this type was introduced to prevent.
#
# Regression coverage for a real defect: normalize_nigerian_phone used \d, which
# is Unicode-aware for str patterns in Python 3, so these inputs were accepted
# and returned as "canonical". The consequences were:
#   1. CanonicalPhone's documented "13 bare digits" contract was false.
#   2. The insert then failed the users CHECK constraint, because Postgres
#      character classes are ASCII-only — an IntegrityError surfacing as a 500 on
#      an unauthenticated endpoint instead of a 422.
#   3. The OTP Redis key became `otp:2348٨…`, reintroducing exactly the format
#      drift the CanonicalPhone type was introduced to prevent.
#
# Fixed in app/core/validators.py with an explicit [0-9] class. These are kept as
# ordinary assertions so the defect cannot come back.

UNICODE_DIGIT_PHONES = [
    ("arabic-indic", "2348" + "٨١٥٣٥٥١٩٧"),
    ("devanagari", "2348" + "९१५३५५१९७"),
    ("bengali", "2348" + "৮১৫৩৫৫১৯৭"),
    ("fullwidth", "２３４８０１１１１２２２２"),
]


@pytest.mark.parametrize(("label", "raw"), UNICODE_DIGIT_PHONES)
def test_validator_rejects_non_ascii_digits(label: str, raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_nigerian_phone(raw)


@pytest.mark.parametrize(("label", "raw"), UNICODE_DIGIT_PHONES)
def test_non_ascii_digits_never_reach_storage(label: str, raw: str) -> None:
    """The general invariant, stated without the validator in the loop.

    Whatever normalize_nigerian_phone hands back must be storable: either it
    rejects the input, or it returns something the CHECK constraint accepts.
    Never a value that would blow up on insert. The positive direction — that
    valid ASCII phones still pass through — is pinned separately by
    test_db_constraint_accepts_every_valid_phone.
    """
    try:
        normalized = normalize_nigerian_phone(raw)
    except ValueError:
        return
    assert CONSTRAINT_PATTERN.match(normalized) is not None


@pytest.mark.parametrize(("label", "raw"), UNICODE_DIGIT_PHONES)
def test_send_otp_schema_rejects_non_ascii_digits(label: str, raw: str) -> None:
    with pytest.raises(ValidationError):
        SendOTPRequest(phone=raw)


# ─── Pydantic validation error codes ─────────────────────────────────────────

# The subset of INVALID_INPUTS that is a string, so it can be passed through a
# schema field without lying to the type checker.
INVALID_STRING_INPUTS = [i for i in INVALID_INPUTS if isinstance(i, str)]
NON_STRING_INPUTS = [i for i in INVALID_INPUTS if not isinstance(i, str)]

# TypeAdapter validates a raw payload the way FastAPI does, which is the only
# honest way to hand a schema a value whose type is deliberately wrong.
_SEND_OTP_ADAPTER: TypeAdapter[SendOTPRequest] = TypeAdapter(SendOTPRequest)


@pytest.mark.parametrize("invalid_input", INVALID_STRING_INPUTS)
def test_phone_field_reports_a_value_error(invalid_input: str) -> None:
    """A malformed number is a `value_error`, not a type or coercion error."""
    with pytest.raises(ValidationError) as raised:
        SendOTPRequest(phone=invalid_input)

    phone_errors = [e for e in raised.value.errors() if e["loc"] == ("phone",)]
    assert phone_errors
    assert all(e["type"] == "value_error" for e in phone_errors)


@pytest.mark.parametrize("invalid_input", NON_STRING_INPUTS)
def test_phone_field_reports_a_value_error_for_non_string_input(invalid_input: object) -> None:
    """Non-strings must also be a `value_error`.

    This matters because normalize_nigerian_phone runs as a BeforeValidator: if a
    non-string ever reached .strip() it would raise AttributeError, which pydantic
    cannot wrap and which reaches the client as an unhandled 500 instead of a 422.
    Validated via TypeAdapter so the wrong type reaches the schema on purpose.
    """
    with pytest.raises(ValidationError) as raised:
        _SEND_OTP_ADAPTER.validate_python({"phone": invalid_input})

    phone_errors = [e for e in raised.value.errors() if e["loc"] == ("phone",)]
    assert phone_errors
    assert all(e["type"] == "value_error" for e in phone_errors)


@pytest.mark.parametrize(("raw_input", "expected_phone"), VALID_INPUTS)
def test_every_phone_schema_collapses_every_format_to_one_value(
    raw_input: str,
    expected_phone: str,
) -> None:
    """A subscriber is one row, whichever endpoint they typed the number into.

    All four schemas must map every spelling to the identical canonical value —
    that shared value is the only reason register, login and the OTP flow agree
    with each other.
    """
    assert (
        RegisterRequest(
            phone=raw_input,
            full_name="Test User",
            password="SecurePassword1!",
            role="customer",
        ).phone
        == expected_phone
    )
    assert LoginRequest(phone=raw_input, password="SecurePassword1!").phone == expected_phone
    assert SendOTPRequest(phone=raw_input).phone == expected_phone
    assert OTPVerifyRequest(phone=raw_input, otp="123456").phone == expected_phone


# ─── RegisterRequest password policy ─────────────────────────────────────────


@pytest.mark.parametrize(
    "weak_password",
    [
        "nodigitshere",
        "NODIGITSHERE",
        "lowercase123",
        "12345678",
        "         ",  # all whitespace satisfies both `any()` checks
    ],
)
def test_register_rejects_passwords_failing_the_policy(weak_password: str) -> None:
    with pytest.raises(ValidationError) as raised:
        RegisterRequest(
            phone="08153551975",
            full_name="Test User",
            password=weak_password,
            role="customer",
        )

    password_errors = [e for e in raised.value.errors() if e["loc"] == ("password",)]
    assert password_errors
    assert all(e["type"] == "value_error" for e in password_errors)


@pytest.mark.parametrize("strong_password", ["SecurePassword1!", "aA1aA1aA1"])
def test_register_accepts_passwords_meeting_the_policy(strong_password: str) -> None:
    request = RegisterRequest(
        phone="08153551975",
        full_name="Test User",
        password=strong_password,
        role="customer",
    )
    assert request.password == strong_password


@pytest.mark.parametrize(
    "candidate",
    ["", "a", "aA", "aA1", "a" * 129],
    ids=["empty", "len_1", "len_2", "len_3", "len_129"],
)
def test_register_password_length_bounds(candidate: str) -> None:
    """min_length=8 / max_length=128 are enforced independently of the policy."""
    with pytest.raises(ValidationError) as raised:
        RegisterRequest(
            phone="08153551975",
            full_name="Test User",
            password=candidate,
            role="customer",
        )
    assert any(e["loc"] == ("password",) for e in raised.value.errors())


def test_register_accepts_password_at_the_maximum_length() -> None:
    candidate = "aA1" + "a" * 125
    assert len(candidate) == 128
    assert (
        RegisterRequest(
            phone="08153551975",
            full_name="Test User",
            password=candidate,
            role="customer",
        ).password
        == candidate
    )


# ─── OTPVerifyRequest otp bounds ─────────────────────────────────────────────


@pytest.mark.parametrize("otp", ["1234", "12345678", "abcdef"])
def test_otp_verify_accepts_otp_within_bounds(otp: str) -> None:
    assert OTPVerifyRequest(phone="08153551975", otp=otp).otp == otp


@pytest.mark.parametrize("otp", ["123", "123456789"])
def test_otp_verify_rejects_otp_outside_bounds(otp: str) -> None:
    with pytest.raises(ValidationError) as raised:
        OTPVerifyRequest(phone="08153551975", otp=otp)
    assert any(e["loc"] == ("otp",) for e in raised.value.errors())
