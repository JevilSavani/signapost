"""Application configuration and runtime limits for Signalpost."""

import os
from pathlib import Path
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load local environment variables from .env if present
load_dotenv()

# Root directory of the repository
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    """Signalpost system configuration with strict competition limits."""

    app_env: str = Field(default_factory=lambda: os.getenv("APP_ENV", "production"))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # File paths
    database_path: Path = Field(
        default_factory=lambda: BASE_DIR / os.getenv("DATABASE_PATH", "data/signalpost.db")
    )
    profiles_export_path: Path = Field(
        default_factory=lambda: BASE_DIR / os.getenv("PROFILES_EXPORT_PATH", "data/company_profiles.jsonl")
    )
    cache_dir: Path = Field(
        default_factory=lambda: BASE_DIR / os.getenv("CACHE_DIR", "data/cache")
    )
    reports_dir: Path = Field(default_factory=lambda: BASE_DIR / "reports")

    # Permitted Official Norwegian Public Data Sources
    brreg_enheter_url: str = Field(
        default_factory=lambda: os.getenv(
            "BRREG_ENHETER_URL", "https://data.brreg.no/enhetsregisteret/api/enheter"
        )
    )
    brreg_regnskap_url: str = Field(
        default_factory=lambda: os.getenv(
            "BRREG_REGNSKAP_URL", "https://data.brreg.no/regnskapsregisteret/regnskap"
        )
    )

    # Competition execution constraints (Hard limits)
    max_outbound_requests: int = Field(
        default_factory=lambda: int(os.getenv("MAX_OUTBOUND_REQUESTS", "2000"))
    )
    max_execution_minutes: int = Field(
        default_factory=lambda: int(os.getenv("MAX_EXECUTION_MINUTES", "45"))
    )
    request_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10.0"))
    )
    rate_limit_rps: float = Field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_RPS", "5.0"))
    )

    # Optional LLM settings (Deterministic heuristic fallback used when unset)
    openai_api_key: str | None = Field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY") or None
    )
    llm_model: str = Field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    declared_max_api_cost: float = Field(
        default_factory=lambda: float(os.getenv("DECLARED_MAX_API_COST", "10.00"))
    )

    def ensure_directories(self) -> None:
        """Create necessary data, cache, and report directories if they do not exist."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles_export_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)


# Global settings singleton
settings = Settings()
settings.ensure_directories()
