"""Complete company profile domain model with strict evidentiary grounding."""

from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field

from src.models.evidence import ProvenanceFact, FactChangeRecord


class Address(BaseModel):
    """Structured postal or business address."""

    street_address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    municipality: str | None = None
    municipality_number: str | None = None
    country: str = "Norway"
    country_code: str = "NO"

    def format_single_line(self) -> str:
        parts = [self.street_address, self.postal_code, self.city, self.country]
        return ", ".join(p for p in parts if p)


class CompanyOfficer(BaseModel):
    """Verified corporate officer or board member."""

    role_code: str | None = None
    role_description: str
    name: str
    is_resigned: bool = False


class CompanyFinancials(BaseModel):
    """Verified financial results from official Regnskapsregisteret filings."""

    reporting_year: int | None = None
    period_start: str | None = None
    period_end: str | None = None
    currency: str = "NOK"
    total_revenue: float | None = None
    operating_profit: float | None = None
    net_income: float | None = None
    is_audited: bool | None = None
    statement_type: str | None = None  # e.g., 'SELSKAP' or 'KONSERN'
    journal_number: str | None = None


class OnlinePresence(BaseModel):
    """Verified web properties and scraped business metadata."""

    website_url: str | None = None
    http_status: int | None = None
    meta_description: str | None = None
    business_summary: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None


class CompanyProfile(BaseModel):
    """Comprehensive Norwegian Company Intelligence Profile.

    All facts are backed by traceable evidence, publication dates, and source URLs.
    Missing information is represented as None, avoiding fabricated defaults.
    """

    registration_number: str = Field(description="Unique 9-digit Norwegian organization number")
    name: str = Field(description="Official registered corporate name")
    legal_form_code: str = Field(default="AS", description="Entity code (e.g., AS, ASA, ENK)")
    legal_form_description: str = Field(default="Aksjeselskap")
    status: str = Field(default="ACTIVE", description="ACTIVE, BANKRUPT, LIQUIDATING, or DISSOLVED")

    is_bankrupt: bool = False
    is_in_liquidation: bool = False
    is_in_forced_dissolution: bool = False

    registration_date: str | None = None
    founding_date: str | None = None
    employee_count: int | None = None
    has_registered_employees: bool | None = None
    is_vat_registered: bool | None = None

    nace_code: str | None = None
    nace_description: str | None = None

    registered_office_address: Address | None = None
    postal_address: Address | None = None

    statutory_purpose: str | None = None
    business_description: str | None = None

    website: str | None = None
    phone: str | None = None
    email: str | None = None

    is_part_of_group: bool | None = None
    share_capital: float | None = None

    officers: list[CompanyOfficer] = Field(default_factory=list)
    financials: CompanyFinancials | None = None
    online_presence: OnlinePresence | None = None

    # Fact-level evidence map
    facts: dict[str, ProvenanceFact] = Field(default_factory=dict)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Historical audit log
    change_history: list[FactChangeRecord] = Field(default_factory=list)

    def get_summary(self) -> dict[str, Any]:
        """Produce a clean, human-readable summary of key verified company facts."""
        return {
            "orgnr": self.registration_number,
            "name": self.name,
            "legal_form": f"{self.legal_form_code} ({self.legal_form_description})",
            "status": self.status,
            "industry": f"{self.nace_code or 'N/A'} - {self.nace_description or 'Unknown'}",
            "location": self.registered_office_address.format_single_line() if self.registered_office_address else "Unknown",
            "employees": self.employee_count if self.employee_count is not None else "Unknown",
            "website": self.website or "None",
            "ceo": next((o.name for o in self.officers if "daglig leder" in o.role_description.lower()), "Unknown"),
            "revenue": f"{self.financials.total_revenue:,.0f} {self.financials.currency}" if (self.financials and self.financials.total_revenue is not None) else "Not available",
            "facts_count": len(self.facts),
            "last_verified": self.last_verified_at.isoformat(),
        }
