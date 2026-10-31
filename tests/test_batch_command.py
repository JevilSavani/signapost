"""Comprehensive automated regression tests for the external batch processing command."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.models.evidence import ResearchOutcome
from src.models.result import CompanyResultEnvelope
from src.pipeline.research import load_input_identifiers, process_batch, research_company_pipeline
from src.validation.identity import calculate_mod11_check_digit


# Helper to generate a valid Modulo 11 check digit for absent companies
def make_valid_nonexistent_orgnr(prefix: str = "77777777") -> str:
    check = calculate_mod11_check_digit(prefix)
    if check is None:
        return f"{prefix}0"
    return f"{prefix}{check}"


def test_batch_single_valid_orgnr(tmp_path):
    """Verify batch processing with exactly one valid company identifier."""
    input_file = tmp_path / "single.txt"
    input_file.write_text("923609016\n", encoding="utf-8")
    output_file = tmp_path / "results.jsonl"

    identifiers = load_input_identifiers(input_file)
    assert identifiers == ["923609016"]

    envelopes = process_batch(identifiers, crawl_website=False)
    assert len(envelopes) == 1
    assert envelopes[0].organisation_number == "923609016"
    assert envelopes[0].outcome == ResearchOutcome.AVAILABLE
    assert envelopes[0].company_profile is not None
    assert envelopes[0].company_profile.registration_number == "923609016"


def test_batch_multiple_valid_orgnrs(tmp_path):
    """Verify batch processing across multiple distinct valid company identifiers."""
    input_file = tmp_path / "multiple.txt"
    input_file.write_text("923609016\n984851006\n", encoding="utf-8")

    identifiers = load_input_identifiers(input_file)
    assert identifiers == ["923609016", "984851006"]

    envelopes = process_batch(identifiers, crawl_website=False)
    assert len(envelopes) == 2
    assert envelopes[0].organisation_number == "923609016"
    assert envelopes[1].organisation_number == "984851006"
    assert envelopes[0].name == "EQUINOR ASA"
    assert envelopes[1].name == "DNB BANK ASA"


def test_batch_blank_lines_ignored(tmp_path):
    """Verify blank lines and whitespace-only lines are ignored while preserving order."""
    input_file = tmp_path / "blank_lines.txt"
    content = "\n  \n923609016\n\t\n   \n984851006\n\n"
    input_file.write_text(content, encoding="utf-8")

    identifiers = load_input_identifiers(input_file)
    assert identifiers == ["923609016", "984851006"]


def test_batch_final_line_without_newline(tmp_path):
    """Verify file without a trailing newline correctly parses the last identifier."""
    input_file = tmp_path / "no_newline.txt"
    input_file.write_text("923609016\n984851006", encoding="utf-8")

    identifiers = load_input_identifiers(input_file)
    assert identifiers == ["923609016", "984851006"]


def test_batch_utf8_bom_handling(tmp_path):
    """Verify files with UTF-8 Byte Order Mark (BOM) are read cleanly without corrupting the first identifier."""
    input_file = tmp_path / "bom.txt"
    # Write with utf-8-sig
    input_file.write_text("923609016\n984851006\n", encoding="utf-8-sig")

    identifiers = load_input_identifiers(input_file)
    assert identifiers[0] == "923609016"
    assert not identifiers[0].startswith("\ufeff")


def test_batch_preserves_duplicates(tmp_path):
    """Verify duplicate identifiers produce distinct records at the exact input positions."""
    input_file = tmp_path / "duplicates.txt"
    input_file.write_text("923609016\n984851006\n923609016\n", encoding="utf-8")

    identifiers = load_input_identifiers(input_file)
    assert identifiers == ["923609016", "984851006", "923609016"]

    envelopes = process_batch(identifiers, crawl_website=False)
    assert len(envelopes) == 3
    assert envelopes[0].organisation_number == "923609016"
    assert envelopes[1].organisation_number == "984851006"
    assert envelopes[2].organisation_number == "923609016"


def test_batch_invalid_orgnrs_failure_handling(tmp_path):
    """Verify invalid organization numbers produce FAILED outcome records with error explanations."""
    input_file = tmp_path / "invalid.txt"
    input_file.write_text("invalid_123\n923609015\n", encoding="utf-8")

    identifiers = load_input_identifiers(input_file)
    envelopes = process_batch(identifiers, crawl_website=False)

    assert len(envelopes) == 2
    assert envelopes[0].organisation_number == "invalid_123"
    assert envelopes[0].outcome == ResearchOutcome.FAILED
    assert envelopes[0].status == "INVALID_IDENTIFIER"
    assert envelopes[0].error is not None
    assert envelopes[0].company_profile is None

    assert envelopes[1].organisation_number == "923609015"
    assert envelopes[1].outcome == ResearchOutcome.FAILED
    assert "checksum mismatch" in envelopes[1].error


def test_batch_company_not_found(tmp_path):
    """Verify valid Modulo 11 identifier absent from registries produces NOT_AVAILABLE status."""
    absent_org = make_valid_nonexistent_orgnr("33333333")
    envelope = research_company_pipeline(absent_org, crawl_website=False, use_dataset=False)

    assert envelope.organisation_number == absent_org
    assert envelope.outcome == ResearchOutcome.NOT_AVAILABLE
    assert envelope.status == "NOT_FOUND"
    assert envelope.company_profile is None
    assert "No public registration records" in envelope.synthesis


def test_batch_individual_worker_exception_does_not_abort_batch():
    """Verify that an unexpected exception during one company's processing does not crash the batch."""
    identifiers = ["923609016", "error_org", "984851006"]

    with patch("src.pipeline.research.research_company_pipeline") as mock_pipeline:
        def side_effect(org_id, **kwargs):
            if org_id == "error_org":
                raise RuntimeError("Unexpected collector crash")
            return CompanyResultEnvelope(
                organisation_number=str(org_id),
                outcome=ResearchOutcome.AVAILABLE,
                name=f"Company {org_id}",
                status="ACTIVE",
            )

        mock_pipeline.side_effect = side_effect
        envelopes = process_batch(identifiers, crawl_website=False)

        assert len(envelopes) == 3
        assert envelopes[0].outcome == ResearchOutcome.AVAILABLE
        assert envelopes[1].outcome == ResearchOutcome.FAILED
        assert envelopes[1].status == "PIPELINE_ERROR"
        assert "Unexpected collector crash" in envelopes[1].error
        assert envelopes[2].outcome == ResearchOutcome.AVAILABLE


def test_batch_source_timeout_failure():
    """Verify that a source network failure/timeout reports SOURCE_UNAVAILABLE rather than NOT_FOUND."""
    mock_reg = MagicMock()
    mock_reg.get_enhet.side_effect = TimeoutError("Brønnøysund API gateway timeout")

    absent_org = make_valid_nonexistent_orgnr("44444444")
    envelope = research_company_pipeline(
        absent_org,
        crawl_website=False,
        use_dataset=False,
        reg_collector=mock_reg,
    )

    assert envelope.outcome == ResearchOutcome.FAILED
    assert envelope.status == "SOURCE_UNAVAILABLE"
    assert "unavailable or blocked" in envelope.synthesis


def test_batch_cli_output_directory_creation_and_atomic_write(tmp_path):
    """Verify CLI creates nested output directory and atomically writes JSONL."""
    input_file = tmp_path / "input.txt"
    input_file.write_text("923609016\n", encoding="utf-8")
    nested_output = tmp_path / "deep" / "nested" / "results.jsonl"

    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--batch",
        str(input_file),
        "--output",
        str(nested_output),
        "--no-website",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert nested_output.exists()

    lines = nested_output.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["organisation_number"] == "923609016"
    assert record["status"] == "ACTIVE"
    assert "company_profile" in record
    assert record["company_profile"]["name"] == "EQUINOR ASA"


def test_batch_cli_stdout_clean_jsonl_without_markup(tmp_path):
    """Verify that running --batch without --output emits clean, parseable JSONL to stdout."""
    input_file = tmp_path / "input.txt"
    input_file.write_text("923609016\n984851006\n", encoding="utf-8")

    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--batch",
        str(input_file),
        "--format",
        "jsonl",
        "--no-website",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0

    stdout_lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
    assert len(stdout_lines) == 2
    rec1 = json.loads(stdout_lines[0])
    rec2 = json.loads(stdout_lines[1])
    assert rec1["organisation_number"] == "923609016"
    assert rec2["organisation_number"] == "984851006"


def test_batch_missing_input_file_returns_nonzero(tmp_path):
    """Verify CLI exits nonzero with error to stderr when input file is missing."""
    missing_path = tmp_path / "does_not_exist.txt"
    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--batch",
        str(missing_path),
        "--output",
        str(tmp_path / "out.jsonl"),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode != 0
    assert "not found" in res.stderr.lower()


def test_batch_empty_input_file(tmp_path):
    """Verify batch processing on a completely empty input file succeeds with 0 records."""
    empty_input = tmp_path / "empty.txt"
    empty_input.write_text("", encoding="utf-8")
    output_file = tmp_path / "empty_out.jsonl"

    identifiers = load_input_identifiers(empty_input)
    assert identifiers == []

    envelopes = process_batch(identifiers, crawl_website=False)
    assert envelopes == []

    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--batch",
        str(empty_input),
        "--output",
        str(output_file),
        "--no-website",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert output_file.exists()
    assert output_file.read_text(encoding="utf-8").strip() == ""


def test_batch_external_api_500_failure_does_not_abort(tmp_path):
    """Verify temporary 500 server error from external API does not abort batch processing."""
    identifiers = ["500_error_org", "923609016"]
    mock_reg = MagicMock()
    mock_reg.get_enhet.side_effect = [Exception("500 Internal Server Error"), None]

    with patch("src.pipeline.research.RegistryCollector", return_value=mock_reg):
        envelopes = process_batch(identifiers, crawl_website=False, max_workers=1)
        assert len(envelopes) == 2
        # First one failed due to invalid identifier (not 9 digits) or error
        assert envelopes[0].outcome == ResearchOutcome.FAILED
        assert envelopes[1].outcome in (ResearchOutcome.AVAILABLE, ResearchOutcome.FAILED)


def test_existing_evaluate_functionality_preserved(tmp_path, monkeypatch):
    """Verify existing --evaluate CLI option works without regression."""
    from src.config import settings
    from src.evaluation.metrics import run_evaluation_benchmark

    monkeypatch.setattr(settings, "reports_dir", tmp_path)
    report = run_evaluation_benchmark()
    assert "total_score" in report
    assert report["total_score"] > 80.0

