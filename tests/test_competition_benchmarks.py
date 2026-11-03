"""Comprehensive regression tests for competition-grade leaderboard optimization.

Covers:
- Subsidiary isolation from parent financials & branding
- Distinction between statutory employee count and group workforce
- Full Norwegian UTF-8 alphabet (Æ, Ø, Å, æ, ø, å) integrity
- Temporary HTTP 500/429 retry backoff in CachedHttpClient
- Field-level quality dashboard generation
"""

import json
from unittest.mock import MagicMock, patch
import pytest
import httpx

from src.collectors.registry import RegistryCollector
from src.extraction.company_facts import extract_profile_from_sources
from src.extraction.synthesis import generate_company_synthesis
from src.models.evidence import FactCategory, ResearchOutcome
from src.models.result import CompanyResultEnvelope
from src.pipeline.research import research_company_pipeline
from src.utils.http_client import CachedHttpClient
from src.validation.identity import validate_orgnr


def test_subsidiary_isolated_from_parent_financials():
    """Verify subsidiary profile does not attribute parent company's financial figures."""
    # Subsidiary: 985224323 (Equinor Energy AS)
    sub_enhet = {
        "organisasjonsnummer": "985224323",
        "navn": "EQUINOR ENERGY AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "erIKonsern": True,
        "overordnetEnhet": "923609016",
    }
    # Filing belonging to parent: 923609016 (must be rejected if fed to subsidiary)
    parent_filing = [
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

    profile = extract_profile_from_sources("985224323", sub_enhet, regnskap_list=parent_filing)
    assert profile.registration_number == "985224323"
    assert profile.financials is None
    assert "total_revenue" not in profile.facts
    assert profile.parent_company_orgnr == "923609016"


def test_statutory_employees_distinct_from_group_workforce():
    """Verify statutory Aa-registeret employees is preserved distinctly from group-wide workforce."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        "antallAnsatte": 21231,
    }
    profile = extract_profile_from_sources("923609016", enhet)
    profile.group_workforce = 25000

    synthesis = generate_company_synthesis(profile)
    assert "21231 registered employees" in synthesis
    assert "**Group-Wide Global Workforce**: ~25,000 employees" in synthesis
    assert profile.employee_count == 21231
    assert profile.group_workforce == 25000


def test_norwegian_utf8_alphabet_integrity():
    """Verify complete UTF-8 fidelity for Norwegian characters: Æ, Ø, Å, æ, ø, å."""
    enhet_utf8 = {
        "organisasjonsnummer": "974760673",
        "navn": "BLÅBÆR, TØRRVALS & VÅR INVESTERING AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "forretningsadresse": {
            "adresse": ["Åkerveien 14B", "Øvre Vågsbygd"],
            "postnummer": "5000",
            "poststed": "BERGEN",
            "kommune": "BERGEN",
            "land": "Norge",
            "landkode": "NO",
        },
        "naeringskode1": {"kode": "01.130", "beskrivelse": "Dyrking av grønnsaker, meloner, røtter og knoller"},
        "formaal": ["Utvikling av bærekraftige næringsmidler og salg av råvarer på det åpne marked."],
    }
    profile = extract_profile_from_sources("974760673", enhet_utf8)

    assert "BLÅBÆR" in profile.name
    assert "TØRRVALS" in profile.name
    assert "Åkerveien" in profile.registered_office_address.street_address
    assert "grønnsaker" in profile.nace_description
    assert "bærekraftige" in profile.statutory_purpose

    synthesis = generate_company_synthesis(profile)
    assert "BLÅBÆR, TØRRVALS & VÅR INVESTERING AS" in synthesis
    assert "bærekraftige næringsmidler" in synthesis


def test_http_client_retries_transient_500_and_429():
    """Verify CachedHttpClient retries on HTTP 500 or 429 status codes with backoff."""
    client = CachedHttpClient(cache_db_path=":memory:")
    mock_send = MagicMock()

    # First attempt: 500 internal error; Second attempt: 200 OK
    resp_500 = httpx.Response(500, request=httpx.Request("GET", "https://api.example.com/data"))
    resp_200 = httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", "https://api.example.com/data"))
    mock_send.side_effect = [resp_500, resp_200]

    with patch.object(client.client, "send", mock_send):
        with patch("time.sleep", return_value=None):
            resp = client.get("https://api.example.com/data", use_cache=False, max_retries=3)
            assert resp.status_code == 200
            assert mock_send.call_count == 2


def test_field_level_quality_dashboard_generation(tmp_path):
    """Verify that generate_field_level_quality_dashboard calculates precision, recall, and coverage."""
    from src.evaluation.metrics import generate_field_level_quality_dashboard

    dashboard = generate_field_level_quality_dashboard()
    assert "fields" in dashboard
    assert dashboard["total_profiles_evaluated"] > 0

    fields = dashboard["fields"]
    assert "legal_name" in fields
    assert fields["legal_name"]["precision"] == 1.0
    assert fields["legal_name"]["coverage"] >= 0.99
    assert fields["legal_name"]["provenance_completeness"] == 1.0
    assert "total_revenue" in fields
    assert fields["total_revenue"]["precision"] == 1.0
