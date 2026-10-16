"""Regression tests for financial extraction correctness, currency fidelity, zero-value preservation, and reporting-period validation."""

import pytest
from src.extraction.company_facts import select_latest_valid_filing, extract_profile_from_sources
from src.models.evidence import FactCategory


def test_select_latest_valid_filing_chronological_order():
    """Verify that the most recent financial statement is selected from multi-year filings."""
    filings = [
        {
            "journalnr": "20240001",
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
            "valuta": "USD",
        },
        {
            "journalnr": "20250001",
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
            "valuta": "USD",
        },
        {
            "journalnr": "20260001",
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
        },
    ]
    latest = select_latest_valid_filing(filings, "923609016")
    assert latest is not None
    assert latest["regnskapsperiode"]["tilDato"] == "2025-12-31"
    assert latest["journalnr"] == "20260001"


def test_select_latest_filing_prefers_konsern_for_same_period():
    """Verify that when both KONSERN and SELSKAP exist for the latest period, KONSERN is preferred."""
    filings = [
        {
            "journalnr": "2026_SELSKAP",
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
        },
        {
            "journalnr": "2026_KONSERN",
            "regnskapstype": "KONSERN",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
        },
    ]
    latest = select_latest_valid_filing(filings, "923609016")
    assert latest is not None
    assert latest["regnskapstype"] == "KONSERN"
    assert latest["journalnr"] == "2026_KONSERN"


def test_suspicious_and_inverted_reporting_periods_rejected():
    """Verify that suspicious or inverted reporting dates are rejected."""
    # Inverted dates: start after end
    inverted = [
        {
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2025-12-31", "tilDato": "2025-01-01"},
            "valuta": "NOK",
        }
    ]
    assert select_latest_valid_filing(inverted, "923609016") is None

    # Missing dates
    missing_dates = [
        {
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2025-01-01"},
            "valuta": "NOK",
        }
    ]
    assert select_latest_valid_filing(missing_dates, "923609016") is None


def test_currency_fidelity_no_guessing_or_conversion():
    """Verify currency is extracted verbatim from the filing without guessing or artificial conversion."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    regnskap_usd = [
        {
            "regnskapstype": "KONSERN",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {"sumDriftsinntekter": 106462000000.0},
                    "driftsresultat": 25353000000.0,
                },
                "aarsresultat": 5058000000.0,
            },
        }
    ]
    profile = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_usd)
    assert profile.financials is not None
    assert profile.financials.currency == "USD"
    assert profile.financials.total_revenue == 106462000000.0
    assert "USD" in profile.facts["total_revenue"].evidence_snippet
    assert profile.facts["total_revenue"].reporting_period_start == "2025-01-01"
    assert profile.facts["total_revenue"].reporting_period_end == "2025-12-31"


def test_genuine_zero_revenue_distinguished_from_missing():
    """Verify that genuine reported 0.0 values are preserved and not replaced with None or omitted."""
    enhet = {
        "organisasjonsnummer": "933034887",
        "navn": "1 BYGG AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
    }
    regnskap_zero_rev = [
        {
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "933034887"},
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "NOK",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {"sumDriftsinntekter": 0.0},
                    "driftsresultat": 0.0,
                },
                "aarsresultat": 0.0,
            },
        }
    ]
    profile = extract_profile_from_sources("933034887", enhet, regnskap_list=regnskap_zero_rev)
    assert profile.financials is not None
    assert profile.financials.total_revenue == 0.0
    assert profile.financials.operating_profit == 0.0
    assert profile.financials.net_income == 0.0
    # Facts must exist and hold genuine 0.0
    assert "total_revenue" in profile.facts
    assert profile.facts["total_revenue"].value == 0.0
    assert profile.facts["operating_profit"].value == 0.0
    assert profile.facts["net_income"].value == 0.0


def test_missing_revenue_is_none_and_not_zero():
    """Verify that when driftsinntekter is empty or omitted, revenue is None and no false 0 fact is published."""
    enhet = {
        "organisasjonsnummer": "927419319",
        "navn": "3D TEGNING AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
    }
    regnskap_no_rev = [
        {
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "927419319"},
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "NOK",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {},  # No revenue reported
                    "driftsresultat": -3148.0,
                },
                "aarsresultat": -4808.0,
            },
        }
    ]
    profile = extract_profile_from_sources("927419319", enhet, regnskap_list=regnskap_no_rev)
    assert profile.financials is not None
    assert profile.financials.total_revenue is None
    assert profile.financials.operating_profit == -3148.0
    assert profile.financials.net_income == -4808.0
    # Must NOT create a total_revenue fact with false 0
    assert "total_revenue" not in profile.facts
    assert "operating_profit" in profile.facts
    assert "net_income" in profile.facts
