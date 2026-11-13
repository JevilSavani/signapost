"""Comprehensive competition requirements test suite.

Verifies all 15 core competition dimensions specified in Requirement 10:
1. Valid Norwegian organization numbers.
2. Invalid checksum and invalid length.
3. Nonexistent organization numbers.
4. Runtime input loading (JSON, JSONL, CSV, TXT, string).
5. Empty batches.
6. Batches exceeding 100 companies.
7. Exactly one result per input.
8. Correct output schema.
9. Clean error handling (no crashes).
10. Source attribution and evidentiary provenance.
11. Wrong-company rejection (Roger Aamodt).
12. Parent-company attribution rejection (Otis).
13. Job expiry handling.
14. News versus statutory announcement classification.
15. Change detection and historical evidence retention.
"""

from datetime import datetime, timezone, date
import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock

from src.collectors.jobs import JobPostingCollector, JobStatus
from src.collectors.news import CompanyNewsCollector
from src.collectors.registry import RegistryCollector
from src.collectors.website import WebsiteCollector
from src.extraction.company_facts import extract_profile_from_sources
from src.extraction.synthesis import generate_company_synthesis
from src.models.company import CompanyNewsItem, CompanyProfile, JobPosting, OnlinePresence
from src.models.evidence import FactCategory, FactChangeRecord, FactStatus, ProvenanceFact, ResearchOutcome
from src.models.result import CompanyResultEnvelope, validate_envelope_schema
from src.pipeline.research import load_input_identifiers, process_batch, research_company_pipeline
from src.updates.refresh import detect_fact_changes
from src.validation.identity import calculate_mod11_check_digit, parse_and_verify_orgnr, validate_orgnr
from src.validation.ownership import (
    EntityOwnershipClassification,
    classify_social_account,
    classify_website_ownership,
)


# 1. Valid Norwegian organization numbers
def test_req1_valid_norwegian_orgnr():
    """Verify that valid 9-digit Norwegian numbers pass Modulo 11 check digit verification."""
    is_valid, clean_id, reason = validate_orgnr("923609016")
    assert is_valid is True
    assert clean_id == "923609016"
    assert "valid" in reason.lower()

    # Formatted variants with leading/trailing spaces or NO prefix
    assert parse_and_verify_orgnr("NO 923 609 016 MVA") == "923609016"
    assert parse_and_verify_orgnr("  923609016  ") == "923609016"


# 2. Invalid checksum and invalid length
def test_req2_invalid_checksum_and_length():
    """Verify that invalid lengths and mismatched check digits fail cleanly."""
    # Invalid length (8 digits)
    v1, _, r1 = validate_orgnr("92360901")
    assert v1 is False
    assert "expected 9 digits, got 8" in r1

    # Invalid length (11 digits)
    v2, _, r2 = validate_orgnr("92360901699")
    assert v2 is False
    assert "expected 9 digits, got 11" in r2

    # Checksum mismatch
    v3, _, r3 = validate_orgnr("923609015")
    assert v3 is False
    assert "checksum mismatch" in r3.lower()

    # Illegal remainder 1 (cannot have valid Modulo 11 check digit)
    v4, _, r4 = validate_orgnr("100000130")
    assert v4 is False
    assert "remainder 1" in r4.lower()


# 3. Nonexistent organization numbers
def test_req3_nonexistent_orgnr_outcome():
    """Verify that nonexistent organization numbers return NOT_AVAILABLE without throwing unhandled exceptions."""
    prefix = "11111111"
    check = calculate_mod11_check_digit(prefix)
    absent_org = f"{prefix}{check}"

    mock_reg = MagicMock(spec=RegistryCollector)
    mock_reg.get_enhet.return_value = None  # 404 from Enhetsregisteret

    envelope = research_company_pipeline(
        absent_org,
        crawl_website=False,
        use_dataset=False,
        reg_collector=mock_reg,
    )
    assert envelope.outcome == ResearchOutcome.NOT_AVAILABLE
    assert envelope.organisation_number == absent_org
    assert envelope.company is None
    assert envelope.status == "NOT_FOUND"
    assert "No public registration records" in (envelope.synthesis or "")


# 4. Runtime input loading
def test_req4_runtime_input_loading_formats(tmp_path: Path):
    """Verify that runtime input can be loaded from TXT, JSON, JSONL, CSV, and inline comma-separated strings."""
    # Plaintext TXT
    txt_file = tmp_path / "batch.txt"
    txt_file.write_text("# Header comment\n923609016\n\"984851006\"\n824832552\n", encoding="utf-8")
    assert load_input_identifiers(txt_file) == ["923609016", "984851006", "824832552"]

    # JSON array
    json_file = tmp_path / "batch.json"
    json_file.write_text(json.dumps(["923609016", {"orgnr": "984851006"}]), encoding="utf-8")
    assert load_input_identifiers(json_file) == ["923609016", "984851006"]

    # JSONL
    jsonl_file = tmp_path / "batch.jsonl"
    jsonl_file.write_text("{\"organisation_number\": \"923609016\"}\n\"984851006\"\n", encoding="utf-8")
    assert load_input_identifiers(jsonl_file) == ["923609016", "984851006"]

    # CSV
    csv_file = tmp_path / "batch.csv"
    csv_file.write_text("organisation_number,name\n923609016,Equinor\n984851006,DNB\n", encoding="utf-8")
    assert load_input_identifiers(csv_file) == ["923609016", "984851006"]

    # Inline comma-separated
    assert load_input_identifiers("923609016, 984851006") == ["923609016", "984851006"]


# 5. Empty batches
def test_req5_empty_batch_handling():
    """Verify that empty batches return an empty list safely without crashing."""
    res = process_batch([], crawl_website=False)
    assert res == []


# 6. Batches exceeding 100 companies (>100 items)
def test_req6_batch_exceeding_100_companies():
    """Verify that batch processing handles batches > 100 items concurrently and returns exact count."""
    prefix = "82483255"
    check = calculate_mod11_check_digit(prefix)
    valid_id = f"{prefix}{check}"

    # 120 input items
    large_batch = [valid_id] * 120

    mock_reg = MagicMock(spec=RegistryCollector)
    mock_reg.get_enhet.return_value = {
        "organisasjonsnummer": valid_id,
        "navn": "BATCH TEST AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
    }
    mock_reg.get_roller.return_value = None
    mock_reg.get_regnskap.return_value = None
    mock_reg.get_underenheter.return_value = None

    envelopes = process_batch(large_batch, crawl_website=False, max_workers=5, reg_collector=mock_reg)
    assert len(envelopes) == 120
    assert all(e.outcome == ResearchOutcome.AVAILABLE for e in envelopes)


# 7. Exactly one result per input
def test_req7_exactly_one_result_per_input():
    """Verify that every single input identifier yields exactly one corresponding result in matching order."""
    inputs = [
        "923609016",
        "invalid-id-1",
        "111111116",
        "invalid-id-2",
        "984851006",
    ]
    envelopes = process_batch(inputs, crawl_website=False)
    assert len(envelopes) == len(inputs)
    for expected_in, env in zip(inputs, envelopes):
        assert env.input_identifier == expected_in


# 8. Correct output schema
def test_req8_correct_output_schema_and_validation():
    """Verify that generated result envelopes strictly comply with the competition output schema."""
    envelope = research_company_pipeline("923609016", crawl_website=False)
    is_valid, errors = validate_envelope_schema(envelope)
    assert is_valid is True, f"Schema validation errors: {errors}"
    assert envelope.organisation_number == "923609016"
    assert envelope.input_identifier == "923609016"
    assert envelope.outcome in (ResearchOutcome.AVAILABLE, ResearchOutcome.NOT_AVAILABLE)
    assert isinstance(envelope.facts, dict)
    assert isinstance(envelope.field_statuses, dict)
    assert isinstance(envelope.sources_consulted, list)


# 9. Clean error handling (no crashes)
def test_req9_clean_error_handling_no_crashes():
    """Verify that corrupt inputs, unexpected registry exceptions, and non-numeric strings do not crash the runner."""
    malformed_inputs = [
        "",
        "   ",
        "???!!!",
        "SELECT * FROM companies",
        "999999999999999999999999",
        "-1",
    ]
    for inp in malformed_inputs:
        envelope = research_company_pipeline(inp)
        assert envelope.outcome == ResearchOutcome.FAILED
        assert envelope.organisation_number == inp.strip()
        assert len(envelope.errors) > 0


# 10. Source attribution and evidentiary provenance
def test_req10_source_attribution_and_provenance():
    """Verify that every fact records source URL, retrieval timestamp, confidence, and evidence snippet."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        "stiftelsesdato": "1972-09-18",
    }
    profile = extract_profile_from_sources("923609016", enhet)

    assert "legal_name" in profile.facts
    fact = profile.facts["legal_name"]
    assert fact.value == "EQUINOR ASA"
    assert fact.source_url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    assert fact.source_type == "official_registry"
    assert fact.confidence == 1.0
    assert fact.retrieval_timestamp is not None
    assert fact.evidence_snippet is not None
    assert "EQUINOR ASA" in fact.evidence_snippet


# 11. Wrong-company rejection (Roger Aamodt)
def test_req11_wrong_company_rejection_roger_aamodt():
    """Verify that social media accounts belonging to sibling companies in conflicting sectors are rejected."""
    result = classify_social_account(
        url="https://facebook.com/rogeraamodteiendom",
        anchor_context="Roger Aamodt Eiendom AS",
        expected_name="ROGER AAMODT BYGG AS",
        expected_orgnr="984851006",
    )
    assert result.classification == EntityOwnershipClassification.REJECTED_WRONG_ENTITY
    assert "conflict" in result.evidence.lower() or "conflict" in result.rejection_reason.lower()


# 12. Parent-company attribution rejection (Otis)
def test_req12_parent_company_attribution_rejection_otis():
    """Verify that global multinational social channels are not attributed to local Norwegian subsidiaries."""
    result = classify_social_account(
        url="https://youtube.com/@OtisElevatorCo",
        anchor_context="Otis Elevator Company (Global)",
        expected_name="OTIS AS",
        expected_orgnr="916288887",
    )
    assert result.classification in (
        EntityOwnershipClassification.REJECTED_WRONG_ENTITY,
        EntityOwnershipClassification.VERIFIED_OFFICIAL_PARENT,
    )
    assert result.classification != EntityOwnershipClassification.VERIFIED_EXACT_ENTITY


# 13. Job expiry handling
def test_req13_job_expiry_handling():
    """Verify that past application deadlines are correctly classified as EXPIRED/CLOSED vs OPEN."""
    collector = JobPostingCollector()

    # Expired deadline
    expired_status = collector._determine_status("2024-01-01", "Utvikler stilling")
    assert expired_status in (JobStatus.EXPIRED, JobStatus.CLOSED)

    # Future deadline
    open_status = collector._determine_status("2028-12-31", "Senior Utvikler")
    assert open_status == JobStatus.OPEN

    # Explicit text indicators
    closed_text_status = collector._determine_status("2028-12-31", "Søknadsfrist utløpt")
    assert closed_text_status == JobStatus.CLOSED


# 14. News versus statutory announcement classification
def test_req14_news_versus_statutory_announcement_classification():
    """Verify that Brønnøysundregistrene Kunngjøringer are classified as statutory announcements, not editorial news."""
    collector = CompanyNewsCollector()

    # 1. Statutory announcement item
    stat_item = CompanyNewsItem(
        headline="EQUINOR ASA: Offisiell kunngjøring – Godkjente årsregnskap",
        canonical_url="https://w2.brreg.no/kunngjoring/hent_en.jsp?orgnr=923609016",
        publication_date="2026-06-15",
        publisher="Brønnøysundregistrene",
        item_type="statutory_announcement",
    )
    assert stat_item.item_type == "statutory_announcement"
    assert "Brønnøysundregistrene" in (stat_item.publisher or "")

    # 2. Editorial news item
    news_item = CompanyNewsItem(
        headline="Equinor completes North Sea maintenance ahead of schedule",
        canonical_url="https://www.equinor.com/news/2026-north-sea-maintenance",
        publication_date="2026-07-20",
        publisher="www.equinor.com",
        item_type="editorial_news",
    )
    assert news_item.item_type == "editorial_news"
    assert "equinor.com" in (news_item.publisher or "")

    # 3. Fact partitioning verification in extraction
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources(
        "923609016",
        enhet,
        announcements_data=[stat_item, news_item],
    )

    # public_announcements must receive the statutory announcement
    assert "public_announcements" in profile.facts
    assert any("Godkjente årsregnskap" in str(h) for h in profile.facts["public_announcements"].value)

    # news_articles must receive ONLY the editorial news item
    assert "news_articles" in profile.facts
    for art in profile.facts["news_articles"].value:
        assert "brreg.no" not in art["url"]
        assert art["publisher"] != "Brønnøysundregistrene"


# 15. Change detection and historical evidence retention
def test_req15_change_detection_and_historical_evidence_retention():
    """Verify that fact changes preserve first_observed_at and append prior evidence into historical_values."""
    now_v1 = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    enhet_v1 = {
        "organisasjonsnummer": "923609016",
        "navn": "STATOIL ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    p1 = extract_profile_from_sources("923609016", enhet_v1)
    p1.facts["legal_name"].first_observed_at = now_v1

    # Update name to EQUINOR ASA
    enhet_v2 = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    p2 = extract_profile_from_sources("923609016", enhet_v2)

    changes = detect_fact_changes(p1, p2)
    name_change = next((c for c in changes if c.fact_key == "legal_name"), None)
    assert name_change is not None
    assert name_change.change_type == FactStatus.CHANGED
    assert name_change.old_value == "STATOIL ASA"
    assert name_change.new_value == "EQUINOR ASA"

    # Historical evidence retention check
    new_name_fact = p2.facts["legal_name"]
    assert new_name_fact.first_observed_at == now_v1
    assert len(new_name_fact.historical_values) >= 1
    assert new_name_fact.historical_values[0]["value"] == "STATOIL ASA"
    assert "evidence_snippet" in new_name_fact.historical_values[0]
