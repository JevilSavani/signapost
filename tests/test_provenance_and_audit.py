"""Regression tests for fact and change-record provenance, audit status grounding, and source counting."""

from datetime import datetime, timezone
import pytest

from src.extraction.company_facts import extract_profile_from_sources
from src.extraction.synthesis import generate_company_synthesis
from src.models.evidence import FactCategory, FactChangeRecord, FactStatus, ProvenanceFact
from src.updates.refresh import detect_fact_changes


def test_change_record_provenance_matches_fact_source_url():
    """Verify that change records use the exact source URL associated with the verified fact value."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }

    # Old profile had no announcements
    old_profile = extract_profile_from_sources("923609016", enhet)

    # New profile discovers public announcements from https://www.equinor.com/news
    new_profile = extract_profile_from_sources("923609016", enhet)
    verified_url = "https://www.equinor.com/news"
    new_profile.facts["public_announcements"] = ProvenanceFact(
        fact_key="public_announcements",
        category=FactCategory.OPERATIONAL,
        value=["Collaborating to develop challenging discoveries", "Aibel awarded contract"],
        source_url=verified_url,
        source_type="verified_website",
        source_title="Company Announcements",
        associated_orgnr="923609016",
        retrieval_timestamp=datetime.now(timezone.utc),
        confidence=0.85,
    )

    changes = detect_fact_changes(old_profile, new_profile)
    ann_change = next((c for c in changes if c.fact_key == "public_announcements"), None)

    assert ann_change is not None
    assert ann_change.change_type == FactStatus.NEW
    # Provenance guarantee: change record must point to the verified fact source URL
    assert ann_change.source_url == verified_url
    assert verified_url in ann_change.explanation
    assert "https://www.equinor.com/news/20261015-completing-electrification-project-troll" not in ann_change.source_url


def test_synthesis_citations_match_structured_facts():
    """Verify that synthesis statements and source references cite the validated structured facts."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("923609016", enhet)

    verified_news_url = "https://www.equinor.com/news"
    profile.facts["public_announcements"] = ProvenanceFact(
        fact_key="public_announcements",
        category=FactCategory.OPERATIONAL,
        value=["Collaborating to develop challenging discoveries"],
        source_url=verified_news_url,
        source_type="verified_website",
        source_title="Company Announcements",
        associated_orgnr="923609016",
        retrieval_timestamp=datetime.now(timezone.utc),
        confidence=0.85,
    )

    synthesis = generate_company_synthesis(profile)

    # Section 5 should reference the verified announcements
    assert '"Collaborating to develop challenging discoveries"' in synthesis
    # Section 8 should cite the exact verified news URL
    assert f"[Company Announcements]({verified_news_url})" in synthesis


def test_missing_audit_evidence_omits_audited_claim():
    """Verify that absent audit evidence in filing omits 'Audited: Yes' claim."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    # Filing without 'revisjon' object
    regnskap_no_revisjon = [
        {
            "regnskapstype": "KONSERN",
            "journalnr": "20261111",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {"sumDriftsinntekter": 1000000.0},
                    "driftsresultat": 200000.0,
                },
                "aarsresultat": 150000.0,
            },
        }
    ]

    profile = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_no_revisjon)

    # Must NOT infer is_audited as True from mere presence of financials
    assert profile.financials is not None
    assert profile.financials.is_audited is None

    synthesis = generate_company_synthesis(profile)
    assert "Audited: Yes" not in synthesis


def test_explicit_audit_status_fidelity():
    """Verify that explicit audit status from filing is correctly represented."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }

    # 1. Explicitly audited filing
    regnskap_audited = [
        {
            "regnskapstype": "KONSERN",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
            "revisjon": {"ikkeRevidertAarsregnskap": False, "fravalgRevisjon": False},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 500000.0}}},
        }
    ]
    profile_audited = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_audited)
    assert profile_audited.financials.is_audited is True
    synthesis_audited = generate_company_synthesis(profile_audited)
    assert "Audited: Yes" in synthesis_audited

    # 2. Explicitly unaudited filing
    regnskap_unaudited = [
        {
            "regnskapstype": "SELSKAP",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "NOK",
            "revisjon": {"ikkeRevidertAarsregnskap": True, "fravalgRevisjon": True},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 200000.0}}},
        }
    ]
    profile_unaudited = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_unaudited)
    assert profile_unaudited.financials.is_audited is False
    synthesis_unaudited = generate_company_synthesis(profile_unaudited)
    assert "Audited: No (unaudited / exemption)" in synthesis_unaudited


def test_accurate_source_counts_with_duplicate_urls():
    """Verify that multiple facts sharing the same source URL do not inflate the unique source count."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("923609016", enhet)

    regnskap_url = "https://data.brreg.no/regnskapsregisteret/regnskap/923609016"
    website_url = "https://www.equinor.com"
    news_url = "https://www.equinor.com/news"

    now = datetime.now(timezone.utc)
    # 4 facts on regnskap_url
    profile.facts["total_revenue"] = ProvenanceFact(
        fact_key="total_revenue", category=FactCategory.FINANCIAL, value=1000.0,
        source_url=regnskap_url, source_type="official_financial_statement", source_title="Brønnøysundregistrene Regnskapsregisteret",
        associated_orgnr="923609016", retrieval_timestamp=now, confidence=1.0,
    )
    profile.facts["operating_profit"] = ProvenanceFact(
        fact_key="operating_profit", category=FactCategory.FINANCIAL, value=200.0,
        source_url=regnskap_url, source_type="official_financial_statement", source_title="Brønnøysundregistrene Regnskapsregisteret",
        associated_orgnr="923609016", retrieval_timestamp=now, confidence=1.0,
    )
    # 2 facts on website_url
    profile.facts["official_website"] = ProvenanceFact(
        fact_key="official_website", category=FactCategory.IDENTITY, value=website_url,
        source_url=website_url, source_type="verified_website", source_title="Verified Official Website",
        associated_orgnr="923609016", retrieval_timestamp=now, confidence=0.85,
    )
    profile.facts["social_media"] = ProvenanceFact(
        fact_key="social_media", category=FactCategory.ONLINE, value=[{"platform": "linkedin"}],
        source_url=website_url, source_type="verified_website", source_title="Verified Social Profiles",
        associated_orgnr="923609016", retrieval_timestamp=now, confidence=0.85,
    )
    # 1 fact on news_url
    profile.facts["public_announcements"] = ProvenanceFact(
        fact_key="public_announcements", category=FactCategory.OPERATIONAL, value=["Ann 1"],
        source_url=news_url, source_type="verified_website", source_title="Company Announcements",
        associated_orgnr="923609016", retrieval_timestamp=now, confidence=0.85,
    )

    profile.sources_consulted = [
        {"source_type": "official_register", "url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016", "status": "available"},
        {"source_type": "official_financial_statement", "url": regnskap_url, "status": "available"},
        {"source_type": "verified_website", "url": website_url, "status": "available"},
        {"source_type": "verified_website", "url": news_url, "status": "available"},
    ]

    synthesis = generate_company_synthesis(profile)

    # There are 4 unique verified endpoints across 8 facts
    assert "across 4 verified sources (4 source records consulted; sum of extracted facts: 8):" in synthesis
    # Ensure regnskap_url appears only once in Section 8
    lines_with_regnskap = [line for line in synthesis.splitlines() if regnskap_url in line]
    assert len(lines_with_regnskap) == 1
    # Ensure website_url appears only once in Section 8
    lines_with_website = [line for line in synthesis.splitlines() if f"({website_url})" in line]
    assert len(lines_with_website) == 1


def test_contradictory_audit_evidence_treated_as_unknown():
    """Verify that contradictory revision indicators result in unknown audit status and omitted claims."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }

    # Case 1: claims unaudited, but auditor report is attached
    regnskap_contradictory_1 = [
        {
            "id": 1,
            "journalnr": "2026111111",
            "regnskapstype": "KONSERN",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "NOK",
            "revisjon": {"ikkeRevidertAarsregnskap": True, "revisjonsberetningForeligger": True},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 500000.0}}},
        }
    ]
    p1 = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_contradictory_1)
    assert p1.financials.is_audited is None
    assert "is_audited" not in p1.facts
    s1 = generate_company_synthesis(p1)
    assert "Audited: Yes" not in s1
    assert "Audited: No" not in s1
    assert "Audit status of latest financial filing could not be established" in s1

    # Case 2: claims audited (ikkeRevidert=False), but also opted out (fravalgRevisjon=True)
    regnskap_contradictory_2 = [
        {
            "id": 2,
            "journalnr": "2026222222",
            "regnskapstype": "KONSERN",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "NOK",
            "revisjon": {"ikkeRevidertAarsregnskap": False, "fravalgRevisjon": True},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 500000.0}}},
        }
    ]
    p2 = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_contradictory_2)
    assert p2.financials.is_audited is None
    assert "is_audited" not in p2.facts
    s2 = generate_company_synthesis(p2)
    assert "Audited: Yes" not in s2
    assert "Audited: No" not in s2


def test_financial_currency_and_unit_preservation():
    """Verify that reported currency and nominal units are preserved, and unrecorded currency is not defaulted to NOK or USD."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }

    # 1. Filing with explicit USD currency
    regnskap_usd = [
        {
            "id": 10,
            "journalnr": "2026635024",
            "regnskapstype": "KONSERN",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": "USD",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {"sumDriftsinntekter": 106462000000.0},
                    "driftsresultat": 25353000000.0,
                },
                "aarsresultat": 5058000000.0,
            },
            "eiendeler": {"sumEiendeler": 131727000000.0},
            "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 40497000000.0}},
        }
    ]
    p_usd = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_usd)
    assert p_usd.financials.currency == "USD"
    assert p_usd.financials.currency_units == "exact"
    assert "USD" in p_usd.facts["total_revenue"].evidence_snippet
    assert "base currency units" in p_usd.facts["total_revenue"].evidence_snippet

    s_usd = generate_company_synthesis(p_usd)
    assert "106,462,000,000 USD (Units: base currency units)" in s_usd
    assert "NOK" not in s_usd

    # 2. Filing without valuta (missing / None) -> must NOT default to NOK or USD
    regnskap_no_currency = [
        {
            "id": 20,
            "journalnr": "2026999999",
            "regnskapstype": "SELSKAP",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "valuta": None,
            "resultatregnskapResultat": {
                "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 5000000.0}}
            },
        }
    ]
    p_none = extract_profile_from_sources("923609016", enhet, regnskap_list=regnskap_no_currency)
    assert p_none.financials.currency is None
    assert "(currency unrecorded)" in p_none.facts["total_revenue"].evidence_snippet

    s_none = generate_company_synthesis(p_none)
    # Check that synthesis does NOT assert NOK or USD
    assert "5,000,000 (currency unrecorded)" in s_none
    assert "5,000,000 NOK" not in s_none
    assert "5,000,000 USD" not in s_none
    assert "Reporting currency and units are unrecorded in official financial statements." in s_none


def test_reconcile_unique_facts_with_sum_of_facts_extracted():
    """Verify that sum of sources_consulted[].facts_extracted matches unique structured facts and distinguishes supplied dataset."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("923609016", enhet)

    now = datetime.now(timezone.utc)
    # Add a supplied dataset seed fact
    dataset_url = "dataset:financial-filer-master-2025.jsonl"
    profile.facts["latest_submitted_accounts"] = ProvenanceFact(
        fact_key="latest_submitted_accounts",
        category=FactCategory.FINANCIAL,
        value="2025",
        source_url=dataset_url,
        source_type="supplied_dataset",
        source_title="Norwegian Financial Filer Master 2025",
        associated_orgnr="923609016",
        retrieval_timestamp=now,
        confidence=0.9,
    )

    reg_url = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    profile.sources_consulted = [
        {"source_type": "supplied_dataset", "url": dataset_url, "status": "available", "facts_extracted": 1},
        {"source_type": "official_registry", "url": reg_url, "status": "available", "facts_extracted": len(profile.facts) - 1},
    ]

    synthesis = generate_company_synthesis(profile)
    total_unique_facts = len(profile.facts)

    # Must distinguish independently verified live sources from supplied dataset
    assert "independently verified live sources and 1 supplied dataset record" in synthesis
    assert f"sum of extracted facts: {total_unique_facts}" in synthesis


def test_synthesis_news_items_keep_article_urls_and_dates():
    """Verify that synthesis Section 5 includes article-level markdown links and publication dates."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("923609016", enhet)
    profile.website = "https://www.equinor.com"

    article_url = "https://www.equinor.com/news/20261026-collaborating-to-develop-challenging-discoveries"
    profile.facts["news_articles"] = ProvenanceFact(
        fact_key="news_articles",
        category=FactCategory.OPERATIONAL,
        value=[
            {
                "title": "Collaborating to develop challenging discoveries",
                "url": article_url,
                "date": "2026-10-26",
            }
        ],
        source_url="https://www.equinor.com/news",
        source_type="verified_website",
        associated_orgnr="923609016",
        retrieval_timestamp=datetime.now(timezone.utc),
    )

    synthesis = generate_company_synthesis(profile)
    assert f"[{'Collaborating to develop challenging discoveries'}]({article_url}) (2026-10-26)" in synthesis


def test_uncertainty_visible_when_fields_unresolved():
    """Verify that unresolved fields like email/phone are reported in Key Information Gaps without speculative excuses."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("923609016", enhet)
    # email and phone are None
    profile.email = None
    profile.phone = None

    synthesis = generate_company_synthesis(profile)
    assert "Official corporate contact email is unrecorded in public registry records." in synthesis
    assert "Official corporate telephone number is unrecorded in public registry records." in synthesis
    assert "All primary corporate, operational, and financial dimensions are grounded with verified data." not in synthesis

