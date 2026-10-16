"""Tests for intelligent fact updates, change detection, and audit trail retention."""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from src.extraction.company_facts import extract_profile_from_sources
from src.models.evidence import FactStatus, ProvenanceFact, FactCategory
from src.storage.repositories import CompanyRepository
from src.updates.refresh import detect_fact_changes


@pytest.fixture
def temp_repo(tmp_path: Path):
    return CompanyRepository(db_path=tmp_path / "test_updates.db")


def test_update_audit_trail_retention(temp_repo: CompanyRepository):
    """Verify that multiple updates retain an immutable audit log of prior values."""
    enhet_v1 = {
        "organisasjonsnummer": "923609016",
        "navn": "STATOIL ASA",  # Historical name
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    p1 = extract_profile_from_sources("923609016", enhet_v1)
    temp_repo.save_profile(p1)

    # Now update name to EQUINOR ASA
    enhet_v2 = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    p2 = extract_profile_from_sources("923609016", enhet_v2)
    changes = detect_fact_changes(p1, p2)
    p2.change_history = [c for c in changes if c.change_type != FactStatus.UNCHANGED]
    temp_repo.save_profile(p2)

    # Verify audit history
    history = temp_repo.get_audit_history("923609016")
    assert len(history) == 1
    assert history[0].fact_key == "legal_name"
    assert history[0].old_value == "STATOIL ASA"
    assert history[0].new_value == "EQUINOR ASA"
    assert history[0].change_type == FactStatus.CHANGED
    assert "updated" in history[0].explanation


def test_unavailable_facts_do_not_overwrite_verified_values():
    """Verify that if an upstream source temporarily omits a fact, it is marked UNAVAILABLE."""
    enhet_v1 = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "hjemmeside": "www.equinor.com",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    enhet_v2 = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        # Website temporarily missing
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }

    p1 = extract_profile_from_sources("923609016", enhet_v1)
    p2 = extract_profile_from_sources("923609016", enhet_v2)

    changes = detect_fact_changes(p1, p2)
    change_map = {c.fact_key: c for c in changes}

    assert "official_website" in change_map
    assert change_map["official_website"].change_type == FactStatus.UNAVAILABLE
    assert change_map["official_website"].old_value == "www.equinor.com"
    assert "Preserving prior verified value" in change_map["official_website"].explanation
