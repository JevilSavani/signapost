"""Fact-level provenance, evidence, and audit models."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class FactCategory(str, Enum):
    IDENTITY = "identity"
    REGISTRATION = "registration"
    CLASSIFICATION = "classification"
    LOCATION = "location"
    OFFICER = "officer"
    FINANCIAL = "financial"
    ONLINE = "online"
    OPERATIONAL = "operational"


class FactStatus(str, Enum):
    NEW = "NEW"
    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ProvenanceFact(BaseModel):
    """Represents a single verified fact with comprehensive evidentiary grounding.

    Each fact links directly to its source URL, retrieval timestamp, publication date,
    reporting period (where applicable), confidence score, and raw evidence snippet.
    """

    fact_key: str = Field(description="Unique key of the fact (e.g. 'legal_name', 'revenue')")
    category: FactCategory = Field(default=FactCategory.IDENTITY)
    value: Any = Field(description="Normalized verified value of the fact")
    source_url: str = Field(description="Exact URL where this fact was retrieved")
    source_type: str = Field(
        default="official_registry",
        description="Source category (e.g. 'official_registry', 'official_financial_statement', 'official_website')",
    )
    retrieval_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO 8601 UTC timestamp of when this fact was extracted",
    )
    publication_date: str | None = Field(
        default=None,
        description="Official publication or registration date of this fact (e.g. '2024-03-15')",
    )
    reporting_period_start: str | None = Field(
        default=None,
        description="Beginning of the financial or operational reporting period (e.g. '2023-01-01')",
    )
    reporting_period_end: str | None = Field(
        default=None,
        description="End of the financial or operational reporting period (e.g. '2023-12-31')",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence level in the validity of this fact (1.0 = official government registry)",
    )
    evidence_snippet: str | None = Field(
        default=None,
        description="Verbatim excerpt or excerpted raw data validating this fact",
    )


class FactChangeRecord(BaseModel):
    """Audit log entry capturing the transition of a fact over time."""

    fact_key: str
    old_value: Any
    new_value: Any
    change_type: FactStatus
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_url: str
    explanation: str
