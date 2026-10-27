"""Regression tests for real-world data quality issues.

Covers:
1. Financial statement selection, reporting period priority, and group scope enforcement.
2. UTF-8 encoding preservation end-to-end without mojibake.
3. Transient failure handling, fact preservation, and failed refresh audit trail.
4. Employee count reporting scopes and provenance grounding.
5. Website business description navigation filtering.
6. Social media ownership verification (outbound website link vs name match alone).
7. News article-level URLs, publication dates, and verification evidence.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.extraction.company_facts import extract_profile_from_sources, select_latest_valid_filing
from src.models.company import CompanyFinancials, OnlinePresence
from src.models.evidence import FactCategory, FactChangeRecord, FactStatus, ProvenanceFact
from src.storage.repositories import CompanyRepository
from src.updates.refresh import detect_fact_changes
from src.utils.http_client import CachedHttpClient
from src.validation.ownership import (
    SocialAccountStatus,
    classify_social_account,
    is_credible_business_description,
    validate_news_item,
)


@pytest.fixture
def temp_repo(tmp_path: Path):
    return CompanyRepository(db_path=tmp_path / "test_regressions.db")


# ---------------------------------------------------------------------------
# 1. Financial Statement Selection & Scope Protection
# ---------------------------------------------------------------------------

def test_select_latest_valid_filing_group_scope_preference():
    """Verify select_latest_valid_filing selects latest period and prefers KONSERN for groups."""
    filings = [
        # 2024 Parent
        {
            "journalnr": 101,
            "regnskapstype": "SELSKAP",
            "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
            "resultatregnskapResultat": {
                "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 50000000000.0}}
            },
        },
        # 2025 Parent (SELSKAP)
        {
            "journalnr": 102,
            "regnskapstype": "SELSKAP",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "resultatregnskapResultat": {
                "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 67956000000.0}}
            },
        },
        # 2025 Consolidated Group (KONSERN)
        {
            "journalnr": 103,
            "regnskapstype": "KONSERN",
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "resultatregnskapResultat": {
                "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 106462000000.0}}
            },
        },
    ]

    selected = select_latest_valid_filing(filings, "923609016", is_part_of_group=True)
    assert selected is not None
    assert selected["journalnr"] == 103
    assert selected["regnskapstype"] == "KONSERN"
    assert selected["regnskapsperiode"]["tilDato"] == "2025-12-31"


def test_reject_financial_overwrite_from_older_period():
    """Verify that an older filing cannot overwrite an established newer verified financial figure."""
    now_utc = datetime.now(timezone.utc)
    p1 = extract_profile_from_sources(
        "923609016",
        {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "erIKonsern": True},
        regnskap_list=[
            {
                "regnskapstype": "KONSERN",
                "valuta": "USD",
                "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 106462000000.0}},
                    "aarsresultat": 5058000000.0,
                },
            }
        ],
    )
    assert p1.facts["total_revenue"].value == 106462000000.0

    # In incoming scrape, only older 2023/2024 filing is present
    p2 = extract_profile_from_sources(
        "923609016",
        {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "erIKonsern": True},
        regnskap_list=[
            {
                "regnskapstype": "KONSERN",
                "valuta": "USD",
                "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 100000.0}},
                    "aarsresultat": 15000.0,
                },
            }
        ],
    )

    changes = detect_fact_changes(p1, p2)
    change_map = {c.fact_key: c for c in changes}

    assert "total_revenue" in change_map
    assert change_map["total_revenue"].change_type == FactStatus.CONFLICTING
    assert "older than verified period" in change_map["total_revenue"].explanation
    # Preserved value on p2
    assert p2.facts["total_revenue"].value == 106462000000.0


def test_reject_selskap_overwriting_konsern_for_group_entity():
    """Verify parent-only SELSKAP statement does not overwrite KONSERN for group company."""
    p_konsern = extract_profile_from_sources(
        "923609016",
        {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "erIKonsern": True},
        regnskap_list=[
            {
                "regnskapstype": "KONSERN",
                "valuta": "USD",
                "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 106462000000.0}},
                },
            }
        ],
    )
    p_selskap = extract_profile_from_sources(
        "923609016",
        {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "erIKonsern": True},
        regnskap_list=[
            {
                "regnskapstype": "SELSKAP",
                "valuta": "USD",
                "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 67956000000.0}},
                },
            }
        ],
    )

    changes = detect_fact_changes(p_konsern, p_selskap)
    change_map = {c.fact_key: c for c in changes}

    assert change_map["total_revenue"].change_type == FactStatus.CONFLICTING
    assert "parent-only (SELSKAP)" in change_map["total_revenue"].explanation
    assert p_selskap.facts["total_revenue"].value == 106462000000.0


# ---------------------------------------------------------------------------
# 2. UTF-8 Preservation End-to-End
# ---------------------------------------------------------------------------

def test_utf8_encoding_preserved_in_http_cache_and_json(tmp_path: Path, temp_repo: CompanyRepository):
    """Verify Norwegian characters (æ, ø, å, quotes) are preserved without mojibake."""
    client = CachedHttpClient(cache_db_path=tmp_path / "cache.db")
    norwegian_payload = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "forretningsadresse": {
            "adresse": ["Bærumsveien 50"],
            "postnummer": "1337",
            "poststed": "BÆRUM",
            "kommune": "BÆRUM",
        },
        "vedtektsfestetFormaal": ["Virksomhet knyttet til utforskning og utvinning av råolje og naturgass."],
    }
    url = "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    client._set_cache(
        url=url,
        status_code=200,
        headers={"Content-Type": "text/html; charset=iso-8859-1"},  # Misleading header
        body=json.dumps(norwegian_payload, ensure_ascii=False),
    )

    resp = client.get(url)
    assert resp.status_code == 200
    assert "BÆRUM" in resp.text
    assert "råolje" in resp.text
    assert "BÃ†RUM" not in resp.text
    assert "rÃ¥olje" not in resp.text

    # Verify profile synthesis preserves UTF-8
    data = resp.json()
    profile = extract_profile_from_sources("923609016", data)
    assert "BÆRUM" in profile.facts["registered_office_address"].value
    assert "råolje" in profile.facts["statutory_purpose"].value

    # Verify SQLite persistence preserves UTF-8
    temp_repo.save_profile(profile)
    retrieved = temp_repo.get_profile("923609016")
    assert retrieved is not None
    assert "BÆRUM" in retrieved.facts["registered_office_address"].value
    assert "råolje" in retrieved.facts["statutory_purpose"].value


# ---------------------------------------------------------------------------
# 3. Transient Failures & Fact Preservation
# ---------------------------------------------------------------------------

def test_transient_failure_preserves_verified_facts():
    """Verify that a missing source response preserves verified facts and logs FAILED_REFRESH."""
    p1 = extract_profile_from_sources(
        "923609016",
        {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "hjemmeside": "www.equinor.com",
            "antallAnsatte": 21231,
        },
        regnskap_list=[
            {
                "regnskapstype": "KONSERN",
                "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 106462000000.0}},
                    "aarsresultat": 5058000000.0,
                },
            }
        ],
    )

    # In next run, regnskap API returned 500 error (transient failure)
    p2 = extract_profile_from_sources(
        "923609016",
        {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "hjemmeside": "www.equinor.com",
            "antallAnsatte": 21231,
        },
        regnskap_list=None,
    )
    p2.sources_consulted = [
        {
            "source_type": "official_financial_statement",
            "url": "https://data.brreg.no/regnskapsregisteret/regnskap/923609016",
            "status": "failed",
            "http_code": 500,
        }
    ]

    changes = detect_fact_changes(p1, p2, preserve_missing_facts=True)
    change_map = {c.fact_key: c for c in changes}

    assert "total_revenue" in change_map
    assert change_map["total_revenue"].change_type == FactStatus.FAILED_REFRESH
    assert "Preserving prior verified value" in change_map["total_revenue"].explanation

    # Ensure facts and financials were preserved on p2
    assert "total_revenue" in p2.facts
    assert p2.facts["total_revenue"].value == 106462000000.0
    assert p2.financials is not None
    assert p2.financials.total_revenue == 106462000000.0

    # On a third run where API succeeds again with identical value:
    p3 = extract_profile_from_sources(
        "923609016",
        {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "hjemmeside": "www.equinor.com",
            "antallAnsatte": 21231,
        },
        regnskap_list=[
            {
                "regnskapstype": "KONSERN",
                "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 106462000000.0}},
                    "aarsresultat": 5058000000.0,
                },
            }
        ],
    )
    changes3 = detect_fact_changes(p2, p3)
    change_map3 = {c.fact_key: c for c in changes3}
    # Verified unchanged, NOT marked as NEW!
    assert change_map3["total_revenue"].change_type == FactStatus.UNCHANGED


# ---------------------------------------------------------------------------
# 4. Employee Count Scopes and Grounding
# ---------------------------------------------------------------------------

def test_employee_count_provenance_and_scope_differentiation():
    """Verify employee count provenance details and rejection of ungrounded dummy count."""
    enhet_data = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "antallAnsatte": 21231,
        "registreringsdatoAntallAnsatteEnhetsregisteret": "2026-10-13",
    }
    p1 = extract_profile_from_sources("923609016", enhet_data)
    emp_fact = p1.facts["employee_count"]
    assert emp_fact.value == 21231
    assert "NAV Aa-registeret" in emp_fact.evidence_snippet
    assert "2026-10-13" in emp_fact.evidence_snippet

    # Attempt to overwrite with dummy 500 from ungrounded source
    p2 = extract_profile_from_sources("923609016", {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"})
    p2.facts["employee_count"] = ProvenanceFact(
        fact_key="employee_count",
        category=FactCategory.OPERATIONAL,
        value=500,
        source_url="https://mock-test.com",
        source_type="mock_test",
        source_title="Mock",
        associated_orgnr="923609016",
        confidence=0.5,
        evidence_snippet="dummy 500",
    )

    changes = detect_fact_changes(p1, p2)
    change_map = {c.fact_key: c for c in changes}
    assert change_map["employee_count"].change_type == FactStatus.CONFLICTING
    assert "lacks official registry authority" in change_map["employee_count"].explanation
    assert p2.facts["employee_count"].value == 21231


# ---------------------------------------------------------------------------
# 5. Website Business Description
# ---------------------------------------------------------------------------

def test_navigation_boilerplate_rejected_for_description():
    """Verify navigation strings like 'Explore Connect Site' are rejected."""
    nav_text = "Explore Connect Site Menu Search Home About Contact"
    assert is_credible_business_description(nav_text) is False

    valid_text = (
        "Equinor is an international energy company committed to long-term value creation "
        "in a low-carbon future, producing oil, gas, and renewable energy."
    )
    assert is_credible_business_description(valid_text) is True

    # Profile synthesis returns substantive description or None
    online_nav = OnlinePresence(
        website_url="https://www.equinor.com",
        is_identity_verified=True,
        meta_description=nav_text,
        business_summary=valid_text,
    )
    p = extract_profile_from_sources(
        "923609016",
        {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"},
        online_data=online_nav,
    )
    # Correctly falls back to substantive business_summary since meta_description is navigation text
    assert p.business_description == valid_text


# ---------------------------------------------------------------------------
# 6. Social Media Ownership Validation
# ---------------------------------------------------------------------------

def test_social_ownership_requires_verified_website_outbound_link():
    """Verify handle name-match alone is unverified without website link."""
    # 1. Name match alone without verified outbound link
    res_unlinked = classify_social_account(
        url="https://www.linkedin.com/company/equinor",
        anchor_context="Equinor Profile",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
        is_linked_from_official_website=False,
    )
    assert res_unlinked.status == SocialAccountStatus.UNVERIFIED
    assert res_unlinked.is_verified is False
    assert "Name match alone without verified corporate website linkage" in res_unlinked.rejection_reason

    # 2. Outbound link from verified company website
    res_linked = classify_social_account(
        url="https://www.linkedin.com/company/equinor",
        anchor_context="Follow Equinor on LinkedIn",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
        is_linked_from_official_website=True,
    )
    assert res_linked.status == SocialAccountStatus.VERIFIED
    assert res_linked.is_verified is True


# ---------------------------------------------------------------------------
# 7. News Items Article-Level URLs and Evidence
# ---------------------------------------------------------------------------

def test_news_articles_retain_article_url_and_evidence():
    """Verify news items retain article-level URLs, publication dates, and evidence."""
    val_res = validate_news_item(
        title="Equinor and partners start production from Johan Sverdrup Phase 2",
        url="https://www.equinor.com/news/2026/02/johan-sverdrup-phase-2-production",
        raw_date="2026-02-15",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
        source_url="https://www.equinor.com/news",
        has_explicit_publication_evidence=True,
    )
    assert val_res.status.value == "verified"
    assert val_res.publication_date == "2026-02-15"
    assert val_res.company_relevance_evidence is not None
    assert "Verified publication date" in val_res.company_relevance_evidence

    # Verify fact synthesis
    online = OnlinePresence(
        website_url="https://www.equinor.com",
        is_identity_verified=True,
        news_items=[{
            "title": val_res.title,
            "url": val_res.url,
            "canonical_url": val_res.canonical_url,
            "publication_date": val_res.publication_date,
            "company_relevance_evidence": val_res.company_relevance_evidence,
            "is_verified": True,
        }],
    )
    profile = extract_profile_from_sources(
        "923609016",
        {"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"},
        online_data=online,
    )
    news_fact = profile.facts["news_articles"]
    assert len(news_fact.value) == 1
    art = news_fact.value[0]
    assert art["url"] == "https://www.equinor.com/news/2026/02/johan-sverdrup-phase-2-production"
    assert art["date"] == "2026-02-15"
    assert "Verified publication date" in art["evidence"]
