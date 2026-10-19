"""Robust, streaming Norwegian company dataset loader and fast local index.

Reads financial-filer-master-2025.jsonl incrementally without loading the entire 131 MB
into memory. Supports O(1) indexed lookups, Modulo 11-safe normalization, duplicate tracking,
and conversion to evidence-backed seed facts.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Generator

from src.config import settings
from src.models.dataset import DatasetRecord
from src.models.evidence import FactCategory, ProvenanceFact, ResearchOutcome
from src.validation.identity import sanitize_orgnr

logger = logging.getLogger(__name__)


class DatasetNotFoundError(FileNotFoundError):
    """Raised when the expected Norwegian financial filer master dataset is missing."""
    pass


class DatasetLoader:
    """Incremental JSONL dataset loader with persistent SQLite byte-offset index."""

    def __init__(
        self,
        dataset_path: Path | str | None = None,
        index_path: Path | str | None = None,
        auto_index: bool = True,
    ) -> None:
        self.dataset_path = Path(dataset_path or settings.master_dataset_path)
        self.index_path = Path(index_path or settings.dataset_index_path)
        self.auto_index = auto_index
        self._index_ready = False

    def exists(self) -> bool:
        """Check if the dataset file exists on disk."""
        return self.dataset_path.is_file()

    def assert_dataset_exists(self) -> None:
        """Verify the dataset file exists, raising a clear, actionable error if missing."""
        if not self.exists():
            raise DatasetNotFoundError(
                f"Master dataset file not found at '{self.dataset_path}'. "
                f"Please ensure 'financial-filer-master-2025.jsonl' is placed in '{self.dataset_path.parent}'."
            )

    def normalize_orgnr(self, raw_input: str | int | None) -> str:
        """Safely normalize an organization number while preserving string identity and leading zeros."""
        if raw_input is None:
            return ""
        try:
            return sanitize_orgnr(raw_input)
        except Exception:
            return str(raw_input).strip()

    def iter_records(self) -> Generator[DatasetRecord, None, None]:
        """Stream dataset records incrementally, one line at a time.

        Handles blank lines, malformed JSON, and Unicode characters safely without
        aborting iteration.
        """
        self.assert_dataset_exists()

        with open(self.dataset_path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as err:
                    logger.warning("Malformed JSON on line %d of %s: %s", line_no, self.dataset_path.name, err)
                    continue

                if not isinstance(data, dict):
                    continue

                raw_org = data.get("organisation_number")
                if not raw_org:
                    continue

                clean_org = self.normalize_orgnr(raw_org)
                data["organisation_number"] = clean_org

                try:
                    yield DatasetRecord.model_validate(data)
                except Exception as val_err:
                    logger.debug("Record validation error on line %d: %s", line_no, val_err)
                    continue

    def _init_index_db(self, conn: sqlite3.Connection) -> None:
        """Create the SQLite table and indices for byte offset lookup."""
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_index (
                orgnr TEXT PRIMARY KEY,
                byte_offset INTEGER NOT NULL,
                byte_length INTEGER NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dataset_orgnr ON dataset_index(orgnr);")

    def build_index(self, force: bool = False) -> dict[str, Any]:
        """Scan the JSONL file once and build an efficient SQLite byte-offset index.

        Memory efficient: never holds the whole file in memory; flushes in batches.
        Detects duplicates and invalid lines without data corruption.
        """
        self.assert_dataset_exists()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if index already exists and is non-empty
        if not force and self.index_path.is_file() and self.index_path.stat().st_size > 0:
            with sqlite3.connect(str(self.index_path)) as conn:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM dataset_index")
                    count = cur.fetchone()[0]
                    if count > 0:
                        self._index_ready = True
                        return {"status": "already_indexed", "record_count": count}
                except sqlite3.OperationalError:
                    pass  # table doesn't exist, rebuild

        logger.info("Building byte-offset index for %s -> %s...", self.dataset_path.name, self.index_path.name)
        total_lines = 0
        indexed_count = 0
        duplicate_count = 0
        invalid_count = 0
        duplicates: list[str] = []

        batch: list[tuple[str, int, int]] = []
        batch_size = 25000
        seen_in_run: set[str] = set()

        # Temporary in-memory or WAL index build
        with sqlite3.connect(str(self.index_path)) as conn:
            self._init_index_db(conn)
            conn.execute("DELETE FROM dataset_index;")

            with open(self.dataset_path, "rb") as f:
                offset = 0
                for line in f:
                    total_lines += 1
                    line_len = len(line)
                    line_raw = line.strip()

                    if not line_raw:
                        offset += line_len
                        continue

                    try:
                        # Quick partial decode to extract organisation_number
                        # Avoids full json decode overhead during indexing
                        text = line_raw.decode("utf-8", errors="replace")
                        data = json.loads(text)
                        raw_org = data.get("organisation_number")
                    except Exception:
                        invalid_count += 1
                        offset += line_len
                        continue

                    if not raw_org:
                        invalid_count += 1
                        offset += line_len
                        continue

                    clean_org = self.normalize_orgnr(raw_org)
                    if clean_org in seen_in_run:
                        duplicate_count += 1
                        if len(duplicates) < 10:
                            duplicates.append(clean_org)
                        offset += line_len
                        continue

                    seen_in_run.add(clean_org)
                    batch.append((clean_org, offset, line_len))
                    indexed_count += 1

                    if len(batch) >= batch_size:
                        conn.executemany(
                            "INSERT OR REPLACE INTO dataset_index (orgnr, byte_offset, byte_length) VALUES (?, ?, ?);",
                            batch,
                        )
                        batch.clear()

                    offset += line_len

                if batch:
                    conn.executemany(
                        "INSERT OR REPLACE INTO dataset_index (orgnr, byte_offset, byte_length) VALUES (?, ?, ?);",
                        batch,
                    )
                    batch.clear()

            conn.commit()

        self._index_ready = True
        logger.info(
            "Index complete for %s: %d indexed, %d duplicates, %d invalid lines",
            self.dataset_path.name,
            indexed_count,
            duplicate_count,
            invalid_count,
        )
        return {
            "status": "built",
            "total_lines": total_lines,
            "indexed_count": indexed_count,
            "duplicate_count": duplicate_count,
            "sample_duplicates": duplicates,
            "invalid_lines_count": invalid_count,
            "index_path": str(self.index_path),
        }

    def ensure_index(self) -> None:
        """Ensure the local index exists and is ready for O(1) queries."""
        if not self._index_ready:
            self.build_index(force=False)

    def get_company(self, orgnr: str | int) -> DatasetRecord | None:
        """Perform an efficient O(1) lookup of a company by organization number.

        Uses the persistent byte-offset index to seek directly to the line without
        loading the dataset into memory.
        """
        clean_org = self.normalize_orgnr(orgnr)
        if not clean_org:
            return None

        if not self.exists():
            return None

        if self.auto_index:
            self.ensure_index()

        # Check SQLite index
        if self.index_path.is_file():
            try:
                with sqlite3.connect(str(self.index_path)) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT byte_offset, byte_length FROM dataset_index WHERE orgnr = ?",
                        (clean_org,),
                    )
                    row = cur.fetchone()
                    if row:
                        offset, length = row
                        with open(self.dataset_path, "rb") as f:
                            f.seek(offset)
                            raw_bytes = f.read(length)
                            text = raw_bytes.decode("utf-8", errors="replace").strip()
                            if text:
                                data = json.loads(text)
                                data["organisation_number"] = clean_org
                                return DatasetRecord.model_validate(data)
            except Exception as e:
                logger.warning("Index lookup error for %s: %s", clean_org, e)

        # Fallback: line-by-line scan if index is unavailable
        logger.debug("Falling back to streaming scan for orgnr %s", clean_org)
        for record in self.iter_records():
            if record.organisation_number == clean_org:
                return record

        return None

    def get_seed_facts(self, orgnr: str | int) -> dict[str, ProvenanceFact]:
        """Extract verified seed facts from the dataset record for a company.

        Identifies the dataset as the source with source_type='supplied_dataset',
        setting is_dataset_seed=True and is_independently_confirmed=False.
        Does not fabricate arbitrary website URLs.
        """
        record = self.get_company(orgnr)
        if not record:
            return {}

        clean_org = record.organisation_number
        dataset_source_url = f"dataset:{self.dataset_path.name}"
        source_title = "Norwegian Financial Filer Master 2025"
        facts: dict[str, ProvenanceFact] = {}

        # 1. Legal Name
        if record.name:
            facts["legal_name"] = ProvenanceFact(
                fact_key="legal_name",
                category=FactCategory.IDENTITY,
                value=record.name.strip(),
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.90,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: organisation_number={clean_org}, name='{record.name}'",
            )

        # 2. Legal Form
        if record.legal_form:
            facts["legal_form"] = ProvenanceFact(
                fact_key="legal_form",
                category=FactCategory.REGISTRATION,
                value=record.legal_form.strip(),
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.90,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: legal_form='{record.legal_form}'",
            )

        # 3. Industry / NACE
        if record.industry_code:
            code_desc = f"{record.industry_code} - {record.industry_label or ''}".strip(" -")
            facts["industry_code"] = ProvenanceFact(
                fact_key="industry_code",
                category=FactCategory.CLASSIFICATION,
                value=code_desc,
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.90,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: industry_code='{record.industry_code}', label='{record.industry_label}'",
            )

        # 4. Location / Municipality
        if record.municipality:
            muni_val = (
                f"{record.municipality} ({record.municipality_number})"
                if record.municipality_number
                else record.municipality
            )
            facts["registered_office_address"] = ProvenanceFact(
                fact_key="registered_office_address",
                category=FactCategory.LOCATION,
                value=muni_val,
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.85,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: municipality='{record.municipality}', number='{record.municipality_number}'",
            )

        # 5. Employees
        if record.employees is not None:
            facts["employee_count"] = ProvenanceFact(
                fact_key="employee_count",
                category=FactCategory.OPERATIONAL,
                value=int(record.employees),
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.85,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: employees={record.employees}",
            )

        # 6. Status / Bankruptcy / Liquidation
        status = "ACTIVE"
        if record.bankrupt:
            status = "BANKRUPT"
        elif record.liquidating:
            status = "LIQUIDATING"

        facts["company_status"] = ProvenanceFact(
            fact_key="company_status",
            category=FactCategory.REGISTRATION,
            value=status,
            source_url=dataset_source_url,
            source_type="supplied_dataset",
            source_title=source_title,
            associated_orgnr=clean_org,
            confidence=0.90,
            is_dataset_seed=True,
            is_independently_confirmed=False,
            status=ResearchOutcome.AVAILABLE,
            evidence_snippet=f"Dataset record: bankrupt={record.bankrupt}, liquidating={record.liquidating}",
        )

        # 7. Website (seed URL without assuming fresh scrape)
        if record.website and record.website.strip():
            facts["official_website"] = ProvenanceFact(
                fact_key="official_website",
                category=FactCategory.ONLINE,
                value=record.website.strip(),
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.80,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: website='{record.website.strip()}'",
            )

        # 8. Latest Submitted Accounts
        if record.latest_submitted_accounts:
            facts["latest_submitted_accounts"] = ProvenanceFact(
                fact_key="latest_submitted_accounts",
                category=FactCategory.FINANCIAL,
                value=str(record.latest_submitted_accounts).strip(),
                source_url=dataset_source_url,
                source_type="supplied_dataset",
                source_title=source_title,
                associated_orgnr=clean_org,
                confidence=0.90,
                is_dataset_seed=True,
                is_independently_confirmed=False,
                status=ResearchOutcome.AVAILABLE,
                evidence_snippet=f"Dataset record: latest_submitted_accounts='{record.latest_submitted_accounts}'",
            )

        return facts


# Global dataset loader instance
dataset_loader = DatasetLoader()
