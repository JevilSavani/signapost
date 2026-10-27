"""Competition Result Envelope and batch execution outcome models."""

from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field, model_validator

from src.models.company import CompanyProfile
from src.models.evidence import ProvenanceFact, ResearchOutcome


class SourceConsulted(BaseModel):
    """Audit record for a consulted source during research."""

    source_type: str
    url: str
    status: ResearchOutcome
    http_code: int | None = None
    facts_extracted: int = 0
    error_message: str | None = None


class CompanyResultEnvelope(BaseModel):
    """Authoritative result envelope complying with Builderr competition contract.

    Guarantees:
    - Exactly one result per input company.
    - Defined outcomes: available, not_available, blocked, not_applicable, ambiguous, failed.
    - Full fact-level evidentiary traceability.
    - Factual synthesis profile.
    - Field-level outcome status breakdown.
    - Input order and identifier preservation.
    """

    organisation_number: str = Field(description="Original or verified Norwegian 9-digit company identifier")
    outcome: ResearchOutcome = Field(description="Overall research outcome for the company")
    name: str | None = Field(default=None, description="Official company name, if verified")
    status: str | None = Field(default=None, description="Legal company status (ACTIVE, BANKRUPT, etc.)")
    company: CompanyProfile | None = Field(default=None, description="Verified company profile, if available")
    company_profile: CompanyProfile | None = Field(default=None, description="Verified company profile complying with evaluation schema")
    facts: dict[str, ProvenanceFact] = Field(default_factory=dict, description="Verified facts grounded in evidence")
    field_statuses: dict[str, ResearchOutcome] = Field(default_factory=dict, description="Field-level outcome breakdown")
    synthesis: str | None = Field(default=None, description="Concise analytical synthesis grounded in verified facts")
    sources_consulted: list[SourceConsulted] = Field(default_factory=list, description="All external/local sources audited")
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    execution_time_seconds: float = Field(default=0.0)
    errors: list[str] = Field(default_factory=list, description="Any isolated errors encountered")
    error: str | None = Field(default=None, description="Structured primary error description if processing fails")

    @model_validator(mode="after")
    def sync_aliases(self) -> "CompanyResultEnvelope":
        if self.company is not None and self.company_profile is None:
            self.company_profile = self.company
        elif self.company_profile is not None and self.company is None:
            self.company = self.company_profile

        if self.errors and self.error is None:
            self.error = self.errors[0]
        elif self.error is not None and not self.errors:
            self.errors = [self.error]
        return self
