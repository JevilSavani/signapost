"""Tests for Norwegian organization number Modulo 11 validation and sanitization."""

import pytest
from src.validation.identity import (
    calculate_mod11_check_digit,
    parse_and_verify_orgnr,
    sanitize_orgnr,
    validate_orgnr,
    InvalidOrgnrError,
)


def test_sanitize_orgnr_variations():
    """Verify stripping of spaces, punctuation, country codes, and MVA suffixes."""
    assert sanitize_orgnr("923 609 016") == "923609016"
    assert sanitize_orgnr("NO 923 609 016 MVA") == "923609016"
    assert sanitize_orgnr("NO-923609016") == "923609016"
    assert sanitize_orgnr("923.609.016") == "923609016"
    assert sanitize_orgnr(923609016) == "923609016"


def test_mod11_calculation():
    """Verify Modulo 11 checksum calculation with known Norwegian entities."""
    # Equinor ASA: 92360901 -> check digit 6
    assert calculate_mod11_check_digit("92360901") == 6
    # DNB Bank ASA: 98485100 -> check digit 6
    assert calculate_mod11_check_digit("98485100") == 6
    # Telenor ASA: 98246371 -> check digit 8
    assert calculate_mod11_check_digit("98246371") == 8


def test_valid_orgnr():
    """Verify validation passes for real Norwegian registration numbers."""
    valid_orgs = [
        "923609016",  # Equinor ASA
        "984851006",  # DNB Bank ASA
        "982463718",  # Telenor ASA
        "886581432",  # Aker ASA
        "920237126",  # Atea ASA
    ]
    for org in valid_orgs:
        is_valid, clean, reason = validate_orgnr(org)
        assert is_valid is True, f"Failed for {org}: {reason}"
        assert clean == org
        assert parse_and_verify_orgnr(org) == org


def test_invalid_length():
    """Verify rejection of organization numbers that are too short or too long."""
    is_valid, _, reason = validate_orgnr("12345678")
    assert is_valid is False
    assert "Invalid length" in reason

    is_valid, _, reason = validate_orgnr("1234567890")
    assert is_valid is False
    assert "Invalid length" in reason


def test_invalid_checksum():
    """Verify rejection when the check digit fails Modulo 11."""
    # Change check digit of Equinor from 6 to 5
    is_valid, _, reason = validate_orgnr("923609015")
    assert is_valid is False
    assert "checksum mismatch" in reason


def test_illegal_mod11_remainder_one():
    """Verify rejection when Modulo 11 produces remainder 1 (which requires check digit 10)."""
    # Find an 8-digit sequence where sum % 11 == 1
    # Weights: [3, 2, 7, 6, 5, 4, 3, 2]
    # If first digit is 4, 4*3 = 12 = 1 mod 11, others 0 -> sum = 12 -> 12 % 11 = 1.
    is_valid, _, reason = validate_orgnr("400000000")
    assert is_valid is False
    assert "remainder 1" in reason


def test_parse_and_verify_raises_on_invalid():
    """Verify InvalidOrgnrError is raised on malformed input."""
    with pytest.raises(InvalidOrgnrError):
        parse_and_verify_orgnr("not-a-number")

    with pytest.raises(InvalidOrgnrError):
        parse_and_verify_orgnr(None)
