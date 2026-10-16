"""Tests for dataset collection, deduplication, JSONL export, and partial profiles."""

import json
from pathlib import Path
import pytest
from src.extraction.company_facts import extract_profile_from_sources
from src.models.evidence import FactStatus
from src.storage.repositories import CompanyRepository
from src.updates.refresh import detect_fact_changes


@pytest.fixture
def temp_repo(tmp_path: Path):
    db_file = tmp_path / "test_signalpost.db"
    return CompanyRepository(db_path=db_file)


def test_deduplication(temp_repo: CompanyRepository):
    """Verify storing the same company number twice updates rather than duplicates."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    p1 = extract_profile_from_sources("923609016", enhet)
    temp_repo.save_profile(p1)
    assert temp_repo.count_profiles() == 1

    # Save same orgnr again
    p2 = extract_profile_from_sources("923609016", enhet)
    temp_repo.save_profile(p2)
    assert temp_repo.count_profiles() == 1


def test_partial_profile_integrity():
    """Verify that a company with only basic registry data is handled cleanly."""
    enhet_sparse = {
        "organisasjonsnummer": "984851006",
        "navn": "DNB BANK ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    profile = extract_profile_from_sources("984851006", enhet_sparse)
    assert profile.name == "DNB BANK ASA"
    assert profile.employee_count is None
    assert profile.website is None
    assert profile.financials is None
    # Verify no fabricated facts
    assert "total_revenue" not in profile.facts
    assert "employee_count" not in profile.facts


def test_fact_change_detection():
    """Verify semantic change detection when facts evolve."""
    enhet_v1 = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "antallAnsatte": 21000,
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    enhet_v2 = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "antallAnsatte": 21231,  # Staff changed
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        "hjemmeside": "www.equinor.com",  # New fact
    }

    p1 = extract_profile_from_sources("923609016", enhet_v1)
    p2 = extract_profile_from_sources("923609016", enhet_v2)

    changes = detect_fact_changes(p1, p2)
    change_map = {c.fact_key: c for c in changes}

    assert "employee_count" in change_map
    assert change_map["employee_count"].change_type == FactStatus.CHANGED
    assert change_map["employee_count"].old_value == 21000
    assert change_map["employee_count"].new_value == 21231

    assert "official_website" in change_map
    assert change_map["official_website"].change_type == FactStatus.NEW

    assert "legal_name" in change_map
    assert change_map["legal_name"].change_type == FactStatus.UNCHANGED


def test_jsonl_export_validity(temp_repo: CompanyRepository, tmp_path: Path):
    """Verify JSONL export is structurally valid and complete."""
    enhet = {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    }
    p = extract_profile_from_sources("923609016", enhet)
    temp_repo.save_profile(p)

    export_file = tmp_path / "profiles_export.jsonl"
    count = temp_repo.export_profiles_to_jsonl(export_file)
    assert count == 1
    assert export_file.exists()

    with open(export_file, "r", encoding="utf-8") as f:
        line = f.readline()
        record = json.loads(line)
        assert record["registration_number"] == "923609016"
        assert record["name"] == "EQUINOR ASA"
        assert "facts" in record
