"""Norwegian Organization Number (organisasjonsnummer) validation and sanitization.

All registered legal entities in Norway have a unique 9-digit registration number
governed by the Brønnøysund Register Centre, with a check digit computed via Modulo 11.
"""

import re
from typing import Tuple


class InvalidOrgnrError(ValueError):
    """Raised when an organization number fails structural or checksum validation."""
    pass


def sanitize_orgnr(raw_input: str | int) -> str:
    """Extract and sanitize a 9-digit Norwegian organization number string.

    Handles common patterns like:
    - '923 609 016'
    - 'NO 923 609 016 MVA'
    - 'NO-923609016'
    - '923.609.016'
    """
    if raw_input is None:
        raise InvalidOrgnrError("Organization number cannot be None")

    text = str(raw_input).strip()
    # Strip common prefixes like 'NO' and suffixes like 'MVA'
    text = re.sub(r"^(NO\s*|\bNO\b)", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"(\s*MVA\b|\bMVA\b)$", "", text, flags=re.IGNORECASE).strip()

    # Extract all consecutive digits
    digits = re.sub(r"\D", "", text)
    return digits


def calculate_mod11_check_digit(eight_digits: str) -> int | None:
    """Compute the 9th check digit for the first 8 digits using Modulo 11.

    Weights: [3, 2, 7, 6, 5, 4, 3, 2]
    Returns the expected check digit (0-9) or None if the remainder is 1 (which requires digit 10).
    """
    if len(eight_digits) != 8 or not eight_digits.isdigit():
        return None

    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(eight_digits, weights))
    remainder = total % 11

    if remainder == 0:
        return 0
    elif remainder == 1:
        # A remainder of 1 results in check digit 10, which cannot be represented as a single digit.
        # Such numbers are intentionally skipped by the Norwegian authorities.
        return None
    else:
        return 11 - remainder


def validate_orgnr(raw_input: str | int) -> Tuple[bool, str, str]:
    """Validate a Norwegian organization number.

    Returns:
        (is_valid, sanitized_orgnr, reason)
    """
    try:
        orgnr = sanitize_orgnr(raw_input)
    except InvalidOrgnrError as e:
        return False, "", str(e)

    if len(orgnr) != 9:
        return False, orgnr, f"Invalid length: expected 9 digits, got {len(orgnr)}"

    expected_check = calculate_mod11_check_digit(orgnr[:8])
    if expected_check is None:
        return False, orgnr, "Invalid Modulo 11 sequence: remainder 1 yields illegal check digit 10"

    actual_check = int(orgnr[8])
    if expected_check != actual_check:
        return (
            False,
            orgnr,
            f"Modulo 11 checksum mismatch: expected {expected_check}, found {actual_check}",
        )

    return True, orgnr, "Valid Norwegian organization number"


def parse_and_verify_orgnr(raw_input: str | int) -> str:
    """Sanitize and strictly verify an organization number, raising InvalidOrgnrError if invalid."""
    is_valid, orgnr, reason = validate_orgnr(raw_input)
    if not is_valid:
        raise InvalidOrgnrError(f"Rejected invalid organization number '{raw_input}': {reason}")
    return orgnr
