"""Data access repository for CompanyProfile, facts, and audit history."""

import json
import logging
from pathlib import Path
from src.config import settings
from src.models.company import CompanyProfile
from src.models.evidence import FactChangeRecord, FactStatus, ProvenanceFact
from src.storage.database import get_db_connection, init_database

logger = logging.getLogger(__name__)


class CompanyRepository:
    """Repository managing persistence to SQLite and export to JSONL."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or settings.database_path)
        init_database(self.db_path)

    def save_profile(self, profile: CompanyProfile) -> None:
        """Atomically persist a CompanyProfile along with all constituent evidentiary facts."""
        orgnr = profile.registration_number
        data_json = profile.model_dump_json()
        address_line = (
            profile.registered_office_address.format_single_line()
            if profile.registered_office_address
            else None
        )

        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()

            # 1. Upsert company summary row
            cursor.execute(
                """
                INSERT INTO companies (
                    orgnr, name, legal_form, status, nace_code, nace_description,
                    address_line, employee_count, website, data_json, updated_at, last_verified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(orgnr) DO UPDATE SET
                    name = excluded.name,
                    legal_form = excluded.legal_form,
                    status = excluded.status,
                    nace_code = excluded.nace_code,
                    nace_description = excluded.nace_description,
                    address_line = excluded.address_line,
                    employee_count = excluded.employee_count,
                    website = excluded.website,
                    data_json = excluded.data_json,
                    updated_at = CURRENT_TIMESTAMP,
                    last_verified_at = CURRENT_TIMESTAMP;
                """,
                (
                    orgnr,
                    profile.name,
                    f"{profile.legal_form_code} ({profile.legal_form_description})",
                    profile.status,
                    profile.nace_code,
                    profile.nace_description,
                    address_line,
                    profile.employee_count,
                    profile.website,
                    data_json,
                ),
            )

            # 2. Upsert constituent facts
            for fact_key, fact in profile.facts.items():
                cursor.execute(
                    """
                    INSERT INTO facts (
                        orgnr, fact_key, category, value_json, source_url, source_type,
                        confidence, publication_date, reporting_period_start, reporting_period_end,
                        evidence_snippet, retrieval_timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(orgnr, fact_key) DO UPDATE SET
                        category = excluded.category,
                        value_json = excluded.value_json,
                        source_url = excluded.source_url,
                        source_type = excluded.source_type,
                        confidence = excluded.confidence,
                        publication_date = excluded.publication_date,
                        reporting_period_start = excluded.reporting_period_start,
                        reporting_period_end = excluded.reporting_period_end,
                        evidence_snippet = excluded.evidence_snippet,
                        retrieval_timestamp = excluded.retrieval_timestamp;
                    """,
                    (
                        orgnr,
                        fact.fact_key,
                        fact.category.value,
                        json.dumps(fact.value),
                        fact.source_url,
                        fact.source_type,
                        fact.confidence,
                        fact.publication_date,
                        fact.reporting_period_start,
                        fact.reporting_period_end,
                        fact.evidence_snippet,
                        fact.retrieval_timestamp.isoformat(),
                    ),
                )

            # 3. Insert change history records
            for change in profile.change_history:
                cursor.execute(
                    """
                    INSERT INTO audit_log (
                        orgnr, fact_key, old_value_json, new_value_json, change_type,
                        source_url, explanation, detected_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        orgnr,
                        change.fact_key,
                        json.dumps(change.old_value),
                        json.dumps(change.new_value),
                        change.change_type.value,
                        change.source_url,
                        change.explanation,
                        change.detected_at.isoformat(),
                    ),
                )

            conn.commit()

    def get_profile(self, orgnr: str) -> CompanyProfile | None:
        """Retrieve a full CompanyProfile by organization number."""
        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data_json FROM companies WHERE orgnr = ?", (str(orgnr).strip(),))
            row = cursor.fetchone()
            if row:
                return CompanyProfile.model_validate_json(row[0])
        return None

    def list_profiles(self, limit: int = 1000, offset: int = 0) -> list[CompanyProfile]:
        """Fetch profiles ordered by creation time."""
        profiles: list[CompanyProfile] = []
        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT data_json FROM companies ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            for row in cursor.fetchall():
                profiles.append(CompanyProfile.model_validate_json(row[0]))
        return profiles

    def count_profiles(self) -> int:
        """Return total number of distinct stored company profiles."""
        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM companies")
            return cursor.fetchone()[0]

    def record_audit_entry(self, orgnr: str, record: FactChangeRecord) -> None:
        """Append an individual change record to the audit log."""
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    orgnr, fact_key, old_value_json, new_value_json, change_type,
                    source_url, explanation, detected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    orgnr,
                    record.fact_key,
                    json.dumps(record.old_value),
                    json.dumps(record.new_value),
                    record.change_type.value,
                    record.source_url,
                    record.explanation,
                    record.detected_at.isoformat(),
                ),
            )
            conn.commit()

    def get_audit_history(self, orgnr: str) -> list[FactChangeRecord]:
        """Retrieve historical change log for a company."""
        changes: list[FactChangeRecord] = []
        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT fact_key, old_value_json, new_value_json, change_type, detected_at, source_url, explanation
                FROM audit_log
                WHERE orgnr = ?
                ORDER BY detected_at ASC
                """,
                (str(orgnr).strip(),),
            )
            for row in cursor.fetchall():
                fact_key, old_v, new_v, change_type, detected_at, source_url, explanation = row
                changes.append(
                    FactChangeRecord(
                        fact_key=fact_key,
                        old_value=json.loads(old_v) if old_v else None,
                        new_value=json.loads(new_v) if new_v else None,
                        change_type=FactStatus(change_type),
                        detected_at=detected_at,
                        source_url=source_url,
                        explanation=explanation,
                    )
                )
        return changes

    def export_profiles_to_jsonl(self, filepath: Path | str | None = None) -> int:
        """Export all company profiles to standard JSONL format for submission/evaluation."""
        target_path = Path(filepath or settings.profiles_export_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        exported_count = 0
        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data_json FROM companies ORDER BY orgnr ASC")

            with open(target_path, "w", encoding="utf-8") as f:
                for row in cursor:
                    # Validate JSON serialization
                    line = row[0].strip()
                    if line:
                        f.write(line + "\n")
                        exported_count += 1

        logger.info("Successfully exported %d profiles to %s", exported_count, target_path)
        return exported_count


# Global repository instance
company_repository = CompanyRepository()
