"""Core validation utilities."""

import re


def normalize_nigerian_phone(v: str) -> str:
    """Normalize any Nigerian phone number to canonical 234XXXXXXXXXX format.

    Supported input formats:
    - 08153551975 (11-digit local format)
    - 2348153551975 (13-digit format without +)
    - +2348153551975 (E.164 international format)
    - Formatted strings with spaces or dashes (e.g. '0815 355 1975', '+234-815-355-1975')

    Returns:
    - 234XXXXXXXXXX (exactly 13 digits)
    """
    # Remove all whitespace, dashes, dots, brackets
    cleaned = re.sub(r"[\s\-\(\)\.]", "", v.strip())

    # Remove leading plus sign if present
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    # Convert 11-digit local format (starts with 0, e.g. 08153551975) to 2348153551975
    if cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = "234" + cleaned[1:]

    # Validate against official Nigerian mobile prefix pattern:
    # Starts with 234, followed by 7, 8, or 9, followed by 9 digits (total 13 digits)
    if not re.match(r"^234[789]\d{9}$", cleaned):
        raise ValueError(
            "Invalid Nigerian phone number. Must be a valid 11-digit local number "
            "(e.g. 08153551975) or 13-digit international format (e.g. 2348153551975)."
        )

    return cleaned
