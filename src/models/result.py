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
    input_identifier: str | None = Field(default=None, description="Original raw input identifier supplied to the runner")
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
        if self.input_identifier is None:
            self.input_identifier = self.organisation_number

        if self.company is not None and self.company_profile is None:
            self.company_profile = self.company
        elif self.company_profile is not None and self.company is None:
            self.company = self.company_profile

        if self.errors and self.error is None:
            self.error = self.errors[0]
        elif self.error is not None and not self.errors:
            self.errors = [self.error]
        return self


def validate_envelope_schema(envelope: Any) -> tuple[bool, list[str]]:
    """Validate that an envelope or raw dictionary strictly satisfies the competition output schema."""
    errors: list[str] = []
    if isinstance(envelope, dict):
        try:
            parsed = CompanyResultEnvelope.model_validate(envelope)
        except Exception as e:
            return False, [f"Schema validation error: {e}"]
    elif isinstance(envelope, CompanyResultEnvelope):
        parsed = envelope
    else:
        return False, ["Invalid envelope object: must be CompanyResultEnvelope or dict"]

    if not parsed.organisation_number:
        errors.append("Missing required field: organisation_number")
    if not parsed.outcome:
        errors.append("Missing required field: outcome")
    if parsed.outcome == ResearchOutcome.AVAILABLE and not parsed.company:
        errors.append("Outcome is 'available' but company profile is missing")
    if not isinstance(parsed.facts, dict):
        errors.append("Field 'facts' must be a dictionary")
    if not isinstance(parsed.field_statuses, dict):
        errors.append("Field 'field_statuses' must be a dictionary")
    if not isinstance(parsed.sources_consulted, list):
        errors.append("Field 'sources_consulted' must be a list")

    return len(errors) == 0, errors
