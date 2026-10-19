"""Automated tests for Norwegian dataset loader, byte-offset index, and error resilience."""

import json
from pathlib import Path
import pytest

from src.models.evidence import FactCategory
from src.storage.dataset_loader import DatasetLoader, DatasetNotFoundError


@pytest.fixture
def sample_jsonl_file(tmp_path: Path):
    """Create a temporary representative JSONL dataset file with valid, invalid, duplicate, and unicode records."""
    file_path = tmp_path / "test_catalogue.jsonl"
    records = [
        # 1. Normal valid record
        {
            "organisation_number": "810034882",
            "name": "SANDNES ELEKTRISKE AS",
            "legal_form": "AS",
            "employees": 11,
            "bankrupt": False,
            "liquidating": False,
            "municipality": "SANDNES",
            "municipality_number": "1108",
            "industry_code": "43.210",
            "industry_label": "Elektrisk installasjonsarbeid",
            "website": "https://sandnes-elektriske.no",
            "latest_submitted_accounts": "2025",
        },
        # 2. Blank line
        "",
        # 3. Unicode characters record (æ, ø, å)
        {
            "organisation_number": "912345672",
            "name": "BLÅBÆR & VÅR AS",
            "legal_form": "AS",
            "employees": 5,
            "bankrupt": False,
            "liquidating": False,
            "municipality": "TROMSØ",
            "municipality_number": "5401",
            "industry_code": "01.130",
            "industry_label": "Dyrking av grønnsaker og bær",
            "website": "",
            "latest_submitted_accounts": "2025",
        },
        # 4. Leading zero registration number (must preserve leading zero)
        {
            "organisation_number": "098765432",
            "name": "NULLSTART HOLDING AS",
            "legal_form": "AS",
            "employees": None,
            "bankrupt": False,
            "liquidating": False,
            "municipality": "OSLO",
            "municipality_number": "0301",
            "industry_code": "64.200",
            "industry_label": "Holdingvirksomhet",
            "website": None,
            "latest_submitted_accounts": "2024",
        },
        # 5. Malformed JSON line
        "INVALID_JSON_LINE {{{",
        # 6. Missing optional fields
        {
            "organisation_number": "984851006",
            "name": "MINIMAL RECORD AS",
            # No employees, no website, no municipality, no industry
        },
        # 7. Duplicate of record 1 (should be tracked, not corrupt index)
        {
            "organisation_number": "810034882",
            "name": "SANDNES ELEKTRISKE AS (DUPLICATE)",
            "legal_form": "AS",
        },
    ]

    with open(file_path, "w", encoding="utf-8") as f:
        for r in records:
            if isinstance(r, dict):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            elif isinstance(r, str):
                f.write(r + "\n")

    return file_path


def test_loading_representative_records(sample_jsonl_file: Path, tmp_path: Path):
    """Verify loading and parsing representative records incrementally."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    records = list(loader.iter_records())

    # 4 valid records (810034882, 912345672, 098765432, 984851006, plus duplicate 810034882)
    assert len(records) == 5
    assert records[0].name == "SANDNES ELEKTRISKE AS"
    assert records[0].employees == 11
    assert records[0].latest_submitted_accounts == "2025"


def test_lookup_by_organisation_number(sample_jsonl_file: Path, tmp_path: Path):
    """Verify O(1) indexed lookup by organisation_number."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    loader.build_index(force=True)

    record = loader.get_company("810034882")
    assert record is not None
    assert record.organisation_number == "810034882"
    assert record.name == "SANDNES ELEKTRISKE AS"
    assert record.municipality == "SANDNES"
    assert record.employees == 11


def test_leading_zero_preservation(sample_jsonl_file: Path, tmp_path: Path):
    """Verify leading zeroes in organisation numbers are strictly preserved and not truncated."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    loader.build_index(force=True)

    record = loader.get_company("098765432")
    assert record is not None
    assert record.organisation_number == "098765432"
    assert record.organisation_number.startswith("0")
    assert len(record.organisation_number) == 9
    assert record.name == "NULLSTART HOLDING AS"


def test_invalid_json_and_missing_fields_resilience(sample_jsonl_file: Path, tmp_path: Path):
    """Verify parser skips malformed lines and handles missing optional fields cleanly."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    stats = loader.build_index(force=True)

    assert stats["invalid_lines_count"] >= 1

    # Record with missing fields
    record = loader.get_company("984851006")
    assert record is not None
    assert record.name == "MINIMAL RECORD AS"
    assert record.employees is None
    assert record.website is None
    assert record.industry_code is None


def test_duplicate_company_records_handling(sample_jsonl_file: Path, tmp_path: Path):
    """Verify duplicates are tracked in audit statistics without corrupting the index."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    stats = loader.build_index(force=True)

    assert stats["duplicate_count"] == 1
    assert "810034882" in stats["sample_duplicates"]


def test_missing_dataset_file_raises_actionable_error(tmp_path: Path):
    """Verify missing dataset file produces a clear, actionable DatasetNotFoundError."""
    missing_file = tmp_path / "non_existent_dataset.jsonl"
    loader = DatasetLoader(dataset_path=missing_file)

    assert loader.exists() is False
    with pytest.raises(DatasetNotFoundError) as exc:
        loader.assert_dataset_exists()

    assert "Master dataset file not found" in str(exc.value)
    assert "financial-filer-master-2025.jsonl" in str(exc.value)


def test_company_number_absent_from_dataset(sample_jsonl_file: Path, tmp_path: Path):
    """Verify lookup for an absent organisation number returns None cleanly."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    loader.build_index(force=True)

    result = loader.get_company("999999995")
    assert result is None


def test_seed_fact_attribution_and_evidence(sample_jsonl_file: Path, tmp_path: Path):
    """Verify seed facts are properly attributed to the dataset without invented URLs."""
    loader = DatasetLoader(dataset_path=sample_jsonl_file, index_path=tmp_path / "idx.sqlite")
    facts = loader.get_seed_facts("810034882")

    assert len(facts) >= 5
    assert facts["legal_name"].value == "SANDNES ELEKTRISKE AS"
    assert facts["legal_name"].source_type == "supplied_dataset"
    assert facts["legal_name"].is_dataset_seed is True
    assert facts["legal_name"].is_independently_confirmed is False
    assert facts["legal_name"].source_url.startswith("dataset:")
    assert "http" not in facts["legal_name"].source_url  # No fake HTTP URLs
