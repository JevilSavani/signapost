"""Automated tests for pipeline execution, error handling, outcome classification, and batch ordering."""

import pytest
from unittest.mock import MagicMock

from src.collectors.registry import RegistryCollector
from src.collectors.website import WebsiteCollector
from src.models.evidence import ResearchOutcome
from src.models.result import CompanyResultEnvelope
from src.pipeline.research import process_batch, research_company_pipeline


def test_official_output_schema_compliance():
    """Verify research result complies with the required result-envelope schema."""
    envelope = research_company_pipeline("923609016", crawl_website=False)

    assert isinstance(envelope, CompanyResultEnvelope)
    assert envelope.organisation_number == "923609016"
    assert envelope.outcome == ResearchOutcome.AVAILABLE
    assert envelope.name == "EQUINOR ASA"
    assert envelope.company is not None
    assert isinstance(envelope.facts, dict)
    assert isinstance(envelope.field_statuses, dict)
    assert envelope.synthesis is not None
    assert len(envelope.synthesis) > 50
    assert isinstance(envelope.sources_consulted, list)
    assert envelope.execution_time_seconds >= 0.0

    # Field-level status presence
    assert "legal_name" in envelope.field_statuses
    assert envelope.field_statuses["legal_name"] == ResearchOutcome.AVAILABLE


def test_empty_research_results_returns_not_available():
    """Verify that a valid Modulo 11 organization number with no registry/catalogue record returns not_available."""
    # 999999995 has valid Modulo 11 check digit (9*3+9*2+9*7+9*6+9*5+9*4+9*3+9*2 = 27+18+63+54+45+36+27+18 = 288 % 11 = 2 -> 11-2 = 9? Wait: 9*3+9*2+9*7+9*6+9*5+9*4+9*3+9*2 = 288, 288 % 11 = 2, 11-2 = 9)
    # Let's use calculate_mod11_check_digit to make a valid non-existent number
    from src.validation.identity import calculate_mod11_check_digit
    prefix = "11111111"
    check = calculate_mod11_check_digit(prefix)
    valid_absent_org = f"{prefix}{check}"

    envelope = research_company_pipeline(valid_absent_org, crawl_website=False, use_dataset=False)
    assert envelope.outcome == ResearchOutcome.NOT_AVAILABLE
    assert envelope.company is None
    assert envelope.organisation_number == valid_absent_org
    assert "No public registration records" in envelope.synthesis


def test_invalid_identifier_returns_failed_outcome_and_preserves_id():
    """Verify invalid organization numbers immediately produce a failed outcome while preserving input identity."""
    invalid_input = "923609015"  # Changed check digit of Equinor from 6 to 5

    envelope = research_company_pipeline(invalid_input)
    assert envelope.outcome == ResearchOutcome.FAILED
    assert envelope.organisation_number == invalid_input
    assert envelope.company is None
    assert len(envelope.errors) >= 1
    assert "checksum mismatch" in envelope.errors[0]


def test_blocked_source_isolation():
    """Verify that when a website is blocked by HTTP 403 or robots.txt, the pipeline gracefully reports blocked."""
    mock_web = MagicMock(spec=WebsiteCollector)
    from src.models.company import OnlinePresence
    mock_web.collect.return_value = OnlinePresence(
        website_url="https://blocked-site.no",
        http_status=403,
        is_identity_verified=False,
    )

    envelope = research_company_pipeline("923609016", web_collector=mock_web)
    assert envelope.outcome == ResearchOutcome.AVAILABLE  # Primary registry succeeded
    assert envelope.field_statuses.get("website") == ResearchOutcome.BLOCKED


def test_ambiguous_website_identity_rejection():
    """Verify that when website content belongs to an unrelated entity, it is flagged as ambiguous."""
    mock_web = MagicMock(spec=WebsiteCollector)
    from src.models.company import OnlinePresence
    mock_web.collect.return_value = OnlinePresence(
        website_url="https://unrelated-domain.no",
        http_status=200,
        is_identity_verified=False,  # Identity check failed
        business_summary="Unrelated corporate group summary",
    )

    envelope = research_company_pipeline("923609016", web_collector=mock_web)
    assert envelope.field_statuses.get("website") == ResearchOutcome.AMBIGUOUS
    # Verify unverified web description is NOT accepted as official description
    assert "Unrelated corporate group summary" not in envelope.company.facts


def test_mixed_success_batch_processing():
    """Verify a batch containing valid, absent, and invalid companies processes completely without aborting."""
    from src.validation.identity import calculate_mod11_check_digit
    prefix = "22222222"
    absent_org = f"{prefix}{calculate_mod11_check_digit(prefix)}"

    batch_inputs = [
        "923609016",  # Equinor (valid)
        "not-a-number",  # Malformed (fails)
        absent_org,  # Absent (not_available)
        "984851006",  # DNB Bank (valid)
    ]

    envelopes = process_batch(batch_inputs, crawl_website=False)

    assert len(envelopes) == 4
    # Exact ordering preserved
    assert envelopes[0].organisation_number == "923609016"
    assert envelopes[0].outcome == ResearchOutcome.AVAILABLE

    assert envelopes[1].organisation_number == "not-a-number"
    assert envelopes[1].outcome == ResearchOutcome.FAILED

    assert envelopes[2].organisation_number == absent_org
    assert envelopes[2].outcome == ResearchOutcome.NOT_AVAILABLE

    assert envelopes[3].organisation_number == "984851006"
    assert envelopes[3].outcome == ResearchOutcome.AVAILABLE


def test_exact_result_count_and_ordering():
    """Verify batch processing guarantees exact 1:1 result count and strictly maintains input ordering."""
    inputs = ["923609016", "984851006", "923609016", "982463718", "999999990"]
    envelopes = process_batch(inputs, crawl_website=False, max_workers=2)

    assert len(envelopes) == len(inputs)
    for expected_id, env in zip(inputs, envelopes):
        assert env.organisation_number == expected_id
