"""Automated tests for factual synthesis generation, evidence grounding, and unknown gap detection."""

from datetime import datetime, timezone
import pytest

from src.extraction.company_facts import extract_profile_from_sources
from src.extraction.synthesis import generate_company_synthesis
from src.models.evidence import FactChangeRecord, FactStatus


def test_synthesis_includes_all_core_dimensions():
    """Verify that synthesis covers activity, legal identity, location, financials, leadership, and evidence."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        "stiftelsesdato": "1972-09-18",
        "registreringsdatoEnhetsregisteret": "1995-03-12",
        "naeringskode1": {"kode": "06.100", "beskrivelse": "Utvinning av råolje"},
        "forretningsadresse": {
            "adresse": ["Forusbeen 50"],
            "postnummer": "4035",
            "poststed": "STAVANGER",
            "kommune": "STAVANGER",
        },
        "antallAnsatte": 21000,
        "kapital": {"belop": 8000000000.0},
    }
    roller = {
        "rollegrupper": [
            {
                "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                "roller": [{"person": {"fornavn": "Anders", "etternavn": "Opedal"}, "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}}],
            }
        ]
    }
    regnskap = [
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

    profile = extract_profile_from_sources("923609016", enhet, roller_data=roller, regnskap_list=regnskap)
    synthesis = generate_company_synthesis(profile)

    # 1. Executive Summary
    assert "EQUINOR ASA" in synthesis
    assert "Allmennaksjeselskap" in synthesis
    assert "Utvinning av råolje" in synthesis

    # 2. Legal Identity & Location
    assert "923609016" in synthesis
    assert "Forusbeen 50" in synthesis
    assert "STAVANGER" in synthesis

    # 3. Financials
    assert "106,462,000,000 USD" in synthesis
    assert "25,353,000,000 USD" in synthesis

    # 4. Leadership
    assert "Anders Opedal" in synthesis

    # 5. Evidence grounding
    assert "data.brreg.no" in synthesis


def test_synthesis_explicitly_identifies_unknowns():
    """Verify that absent information is identified as explicit data gaps without guessing."""
    enhet_minimal = {
        "organisasjonsnummer": "984851006",
        "navn": "DNB BANK ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        # No website, no staff, no financials
    }
    profile = extract_profile_from_sources("984851006", enhet_minimal)
    synthesis = generate_company_synthesis(profile)

    # Must contain unknowns section
    assert "Key Information Gaps & Unknowns" in synthesis
    assert "Employee count is unrecorded" in synthesis
    assert "No official corporate domain" in synthesis
    assert "No public financial statements" in synthesis


def test_synthesis_includes_historical_changes():
    """Verify that changes from audit history are included in the synthesis."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("923609016", enhet)

    audit_entry = FactChangeRecord(
        fact_key="employee_count",
        old_value=20000,
        new_value=21231,
        change_type=FactStatus.CHANGED,
        detected_at=datetime.now(timezone.utc),
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        explanation="Staff increased from 20000 to 21231 according to updated NAV register filing.",
    )
    synthesis = generate_company_synthesis(profile, audit_history=[audit_entry])

    assert "Profile Changes & Historical Audit" in synthesis
    assert "Staff increased from 20000 to 21231" in synthesis
