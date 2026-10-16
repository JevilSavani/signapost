"""SQLite database schema management and connection factory."""

import sqlite3
from pathlib import Path
from src.config import settings


def get_db_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return an optimized connection to the SQLite database."""
    target_path = Path(db_path or settings.database_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target_path))
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_database(db_path: Path | str | None = None) -> None:
    """Initialize all relational tables, indices, and constraints."""
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Main company profile table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                orgnr TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                legal_form TEXT,
                status TEXT NOT NULL,
                nace_code TEXT,
                nace_description TEXT,
                address_line TEXT,
                employee_count INTEGER,
                website TEXT,
                data_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);")

        # 2. Granular evidentiary facts table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orgnr TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                category TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                publication_date TEXT,
                reporting_period_start TEXT,
                reporting_period_end TEXT,
                evidence_snippet TEXT,
                retrieval_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(orgnr, fact_key),
                FOREIGN KEY(orgnr) REFERENCES companies(orgnr) ON DELETE CASCADE
            );
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_facts_orgnr ON facts(orgnr);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(fact_key);")

        # 3. Comprehensive audit log / change history table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                orgnr TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                old_value_json TEXT,
                new_value_json TEXT,
                change_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                explanation TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(orgnr) REFERENCES companies(orgnr) ON DELETE CASCADE
            );
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_orgnr ON audit_log(orgnr);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_detected ON audit_log(detected_at);")

        conn.commit()
