"""Dataset record domain model for the Norwegian financial filer catalogue."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class DatasetRecord(BaseModel):
    """Represents a validated company record from financial-filer-master-2025.jsonl.

    Acts as the initial catalogue seed and enrichment source for Norwegian companies.
    """

    model_config = ConfigDict(extra="ignore")

    organisation_number: str = Field(description="Norwegian 9-digit registration number")
    name: str = Field(description="Registered entity name")
    legal_form: str | None = Field(default=None, description="Legal form code (e.g. AS, ASA, ENK)")
    industry_code: str | None = Field(default=None, description="NACE industry code (e.g. 43.210)")
    industry_label: str | None = Field(default=None, description="NACE industry description")
    municipality: str | None = Field(default=None, description="Municipality name (e.g. SANDNES)")
    municipality_number: str | None = Field(default=None, description="Municipality number (e.g. 1108)")
    employees: int | None = Field(default=None, description="Reported employee count")
    website: str | None = Field(default=None, description="Reported company website")
    bankrupt: bool | None = Field(default=None, description="Bankruptcy indicator")
    liquidating: bool | None = Field(default=None, description="Liquidation indicator")
    latest_submitted_accounts: str | None = Field(
        default=None, description="Latest submitted accounting year (e.g. '2025')"
    )
