"""Automated tests for Signalpost Web UI, API endpoints, and standalone HTML report generator."""

from fastapi.testclient import TestClient
import pytest

from src.models.company import Address, CompanyFinancials, CompanyProfile, OnlinePresence
from src.models.evidence import FactCategory, ProvenanceFact, ResearchOutcome
from src.models.result import CompanyResultEnvelope
from src.ui.app import app
from src.ui.export_html import generate_standalone_html_report


@pytest.fixture
def sample_envelope() -> CompanyResultEnvelope:
    """Fixture providing a rich sample company envelope."""
    facts = {
        "legal_name": ProvenanceFact(
            fact_key="legal_name",
            category=FactCategory.IDENTITY,
            value="EQUINOR ASA",
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
            source_type="official_registry",
            associated_orgnr="923609016",
        ),
        "total_revenue": ProvenanceFact(
            fact_key="total_revenue",
            category=FactCategory.FINANCIAL,
            value=106462000000.0,
            source_url="https://data.brreg.no/regnskapsregisteret/regnskap/923609016",
            source_type="official_financial_statement",
            associated_orgnr="923609016",
        ),
    }

    profile = CompanyProfile(
        registration_number="923609016",
        name="EQUINOR ASA",
        legal_form_code="ASA",
        legal_form_description="Allmennaksjeselskap",
        status="ACTIVE",
        employee_count=21000,
        registered_office_address=Address(street_address="Forusbeen 50", postal_code="4035", city="STAVANGER"),
        financials=CompanyFinancials(
            total_revenue=106462000000.0,
            operating_profit=25353000000.0,
            total_assets=120000000000.0,
            total_equity=50000000000.0,
            total_liabilities=70000000000.0,
            currency="USD",
            is_audited=True,
        ),
        facts=facts,
        synthesis="### 1. Executive Summary & Core Activity\nEQUINOR ASA is an energy company.",
    )

    return CompanyResultEnvelope(
        organisation_number="923609016",
        outcome=ResearchOutcome.AVAILABLE,
        name="EQUINOR ASA",
        status="ACTIVE",
        company=profile,
        facts=facts,
        synthesis="### 1. Executive Summary & Core Activity\nEQUINOR ASA is an energy company.",
    )


def test_standalone_html_report_generation(sample_envelope):
    """Verify that standalone HTML report contains key elements and balanced balance sheet badge."""
    html_out = generate_standalone_html_report(sample_envelope)

    assert "<!DOCTYPE html>" in html_out
    assert "EQUINOR ASA" in html_out
    assert "923609016" in html_out
    assert "Modulo 11 Verified" in html_out
    assert "Balance Sheet Balanced" in html_out
    assert "106,462,000,000 USD" in html_out
    assert "Forusbeen 50" in html_out
    assert "Evidence & Provenance Traceability" in html_out


def test_api_health_endpoint():
    """Verify that /api/health returns 200 OK."""
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["agent"] == "Signalpost"


def test_api_index_endpoint():
    """Verify that / serves the interactive single-page application dashboard."""
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Signalpost Intelligence Agent" in resp.text
    assert "orgnrInput" in resp.text
    assert "verifyModulo11" in resp.text


def test_api_status_endpoint():
    """Verify that /api/status returns database counts and budget metrics."""
    client = TestClient(app)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "stored_profiles_count" in data
    assert "metrics" in data


def test_api_research_invalid_orgnr_rejected():
    """Verify that /api/research/{orgnr} strictly validates Modulo 11 and rejects bad numbers."""
    client = TestClient(app)
    resp = client.get("/api/research/123456789")
    assert resp.status_code == 400
    assert "Invalid Norwegian organization number" in resp.json()["detail"]
