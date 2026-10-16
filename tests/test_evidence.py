"""Tests for fact-level provenance and evidence grounding."""

from datetime import datetime, timezone
import pytest
from src.models.evidence import FactCategory, FactStatus, ProvenanceFact, FactChangeRecord


def test_provenance_fact_creation():
    """Verify that every fact captures source URL, timestamp, confidence, and snippet."""
    fact = ProvenanceFact(
        fact_key="legal_name",
        category=FactCategory.IDENTITY,
        value="EQUINOR ASA",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        publication_date="1995-03-12",
        confidence=1.0,
        evidence_snippet="navn: EQUINOR ASA",
    )
    assert fact.fact_key == "legal_name"
    assert fact.value == "EQUINOR ASA"
    assert fact.source_url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    assert fact.confidence == 1.0
    assert fact.publication_date == "1995-03-12"
    assert fact.retrieval_timestamp <= datetime.now(timezone.utc)


def test_financial_fact_reporting_period():
    """Verify financial facts retain explicit reporting periods."""
    fact = ProvenanceFact(
        fact_key="total_revenue",
        category=FactCategory.FINANCIAL,
        value=107174000000.0,
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/923609016",
        reporting_period_start="2023-01-01",
        reporting_period_end="2023-12-31",
        confidence=1.0,
        evidence_snippet="sumDriftsinntekter: 107174000000.0 USD",
    )
    assert fact.reporting_period_start == "2023-01-01"
    assert fact.reporting_period_end == "2023-12-31"
    assert fact.value == 107174000000.0


def test_fact_change_record():
    """Verify change audit records accurately describe state transitions."""
    record = FactChangeRecord(
        fact_key="employee_count",
        old_value=21000,
        new_value=21231,
        change_type=FactStatus.CHANGED,
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        explanation="Staff increased from 21000 to 21231 according to updated NAV register filing.",
    )
    assert record.change_type == FactStatus.CHANGED
    assert record.old_value == 21000
    assert record.new_value == 21231
