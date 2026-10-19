"""Regression tests auditing source attribution, evidence-grounded synthesis, and fact identity integrity."""

import pytest
from unittest.mock import MagicMock

from src.models.company import CompanyProfile, Address, CompanyFinancials, OnlinePresence, CompanyOfficer
from src.models.evidence import FactCategory, ProvenanceFact, ResearchOutcome
from src.extraction.company_facts import extract_profile_from_sources
from src.extraction.synthesis import generate_company_synthesis
from src.pipeline.research import research_company_pipeline


def test_financial_statement_adapter_facts_extracted_reconciliation():
    """Verify that financial statement adapter counts only genuinely parsed facts."""
    mock_reg = MagicMock()
    mock_reg.get_enhet.return_value = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allment aksjeselskap"},
        "konkurs": False,
        "underAvvikling": False,
    }
    mock_reg.get_roller.return_value = None
    mock_reg.get_regnskap.return_value = [
        {
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
            "valuta": "NOK",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {"sumDriftsinntekter": 100000.0},
                    "driftsresultat": 20000.0,
                },
                "aarsresultat": 15000.0,
            },
            "eiendeler": {"sumEiendeler": 500000.0},
            "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 300000.0}},
        }
    ]
    mock_reg.get_underenheter.return_value = []

    mock_web = MagicMock()
    mock_web.collect.return_value = OnlinePresence(http_status=404)

    envelope = research_company_pipeline(
        "923609016",
        crawl_website=False,
        reg_collector=mock_reg,
        web_collector=mock_web,
        use_dataset=False,
    )

    fin_source = next(
        s for s in envelope.sources_consulted if s.source_type == "official_financial_statement"
    )
    assert fin_source.status == ResearchOutcome.AVAILABLE
    assert fin_source.facts_extracted == 5  # revenue, operating_profit, net_income, total_assets, total_equity
    assert envelope.field_statuses["financials"] == ResearchOutcome.AVAILABLE
    assert "total_revenue" in envelope.facts
    assert envelope.facts["total_revenue"].value == 100000.0
    assert envelope.facts["total_revenue"].source_type == "official_financial_statement"


def test_financial_statement_empty_filing_reports_not_available_and_zero_facts():
    """Verify that an HTTP 200 response with no parseable figures reports NOT_AVAILABLE with 0 facts."""
    mock_reg = MagicMock()
    mock_reg.get_enhet.return_value = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allment aksjeselskap"},
        "konkurs": False,
        "underAvvikling": False,
    }
    mock_reg.get_roller.return_value = None
    mock_reg.get_regnskap.return_value = []  # HTTP 200 returned empty list
    mock_reg.get_underenheter.return_value = []

    envelope = research_company_pipeline(
        "923609016",
        crawl_website=False,
        reg_collector=mock_reg,
        use_dataset=False,
    )

    fin_source = next(
        s for s in envelope.sources_consulted if s.source_type == "official_financial_statement"
    )
    assert fin_source.status == ResearchOutcome.NOT_AVAILABLE
    assert fin_source.facts_extracted == 0
    assert envelope.field_statuses["financials"] == ResearchOutcome.NOT_AVAILABLE


def test_evidence_grounded_synthesis_no_unsupported_claims():
    """Ensure synthesis contains zero speculative excuses for missing data and only fact-grounded claims."""
    profile = CompanyProfile(
        registration_number="923609016",
        name="EQUINOR ASA",
        legal_form_code="ASA",
        legal_form_description="Allment aksjeselskap",
        status="ACTIVE",
        employee_count=None,  # Missing
        website=None,         # Missing
        financials=None,      # Missing
        is_part_of_group=None,# Unrecorded
        facts={
            "legal_name": ProvenanceFact(
                fact_key="legal_name",
                category=FactCategory.IDENTITY,
                value="EQUINOR ASA",
                source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
                source_type="official_registry",
                associated_orgnr="923609016",
            ),
        },
    )

    synthesis = generate_company_synthesis(profile)

    # 1. No speculative excuses
    assert "(common for holding" not in synthesis
    assert "SPV" not in synthesis
    assert "(typical for single-person" not in synthesis
    assert "Independent entity" not in synthesis  # Not specified in registry

    # 2. Distinguishes missing from negative information
    assert "Not available in public records" in synthesis
    assert "No registered employees" not in synthesis
    assert "Information on website, job postings, or announcements is not available from verified sources." in synthesis

    # 3. Exact fact count reconciliation
    assert "Verified against 1 structured evidentiary facts across 1 verified sources" in synthesis


def test_fact_identity_integrity_enforcement():
    """Verify that all facts must contain the correct organisation number and mismatches are rejected."""
    enhet_data = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allment aksjeselskap"},
        "antallAnsatte": 15000,
    }

    # Injecting seed facts where one has a mismatched orgnr
    corrupted_seed_facts = {
        "legal_form": ProvenanceFact(
            fact_key="legal_form",
            category=FactCategory.REGISTRATION,
            value="ASA",
            source_url="dataset:test.jsonl",
            source_type="supplied_dataset",
            associated_orgnr="999999999",  # WRONG ORGNR!
        )
    }

    profile = extract_profile_from_sources(
        expected_orgnr="923609016",
        enhet_data=enhet_data,
        seed_facts=corrupted_seed_facts,
    )

    # Corrupted fact with mismatched orgnr must be rejected
    assert "legal_form" not in profile.facts or profile.facts["legal_form"].associated_orgnr == "923609016"
    for fact in profile.facts.values():
        assert fact.associated_orgnr == "923609016"


def test_sources_consulted_extraction_counts_strictly_match_facts():
    """Verify that for every source in sources_consulted, facts_extracted equals matching facts in profile.facts."""
    mock_reg = MagicMock()
    mock_reg.get_enhet.return_value = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allment aksjeselskap"},
        "konkurs": False,
        "underAvvikling": False,
        "antallAnsatte": 500,
        "hjemmeside": "www.equinor.com",
    }
    mock_reg.get_roller.return_value = {
        "rollegrupper": [
            {
                "type": {"kode": "LEDELSE", "beskrivelse": "Ledelse"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}},
                    }
                ],
            }
        ]
    }
    mock_reg.get_regnskap.return_value = None
    mock_reg.get_underenheter.return_value = []

    envelope = research_company_pipeline(
        "923609016",
        crawl_website=False,
        reg_collector=mock_reg,
        use_dataset=False,
    )

    for sc in envelope.sources_consulted:
        matching = [
            f for f in envelope.facts.values()
            if f.source_url == sc.url or f.source_type == sc.source_type
        ]
        assert sc.facts_extracted == len(matching), (
            f"Source {sc.source_type} ({sc.url}) reported {sc.facts_extracted} facts, "
            f"but {len(matching)} matching facts were found in envelope.facts"
        )
        if sc.facts_extracted > 0:
            assert sc.status == ResearchOutcome.AVAILABLE
        else:
            assert sc.status in (ResearchOutcome.NOT_AVAILABLE, ResearchOutcome.BLOCKED, ResearchOutcome.FAILED)
