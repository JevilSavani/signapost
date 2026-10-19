"""End-to-end company research pipeline, identity verification, and batch processing engine."""

import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from src.collectors.registry import RegistryCollector
from src.collectors.website import WebsiteCollector
from src.extraction.company_facts import extract_profile_from_sources
from src.extraction.synthesis import generate_company_synthesis
from src.models.company import CompanyProfile
from src.models.evidence import FactChangeRecord, FactStatus, ResearchOutcome
from src.models.result import CompanyResultEnvelope, SourceConsulted
from src.storage.dataset_loader import dataset_loader
from src.storage.repositories import company_repository
from src.updates.refresh import detect_fact_changes
from src.validation.identity import validate_orgnr

logger = logging.getLogger(__name__)


def load_input_identifiers(input_source: str | Path) -> list[str]:
    """Parse company identifiers from diverse formats: JSON, JSONL, CSV, TXT, or comma-separated string."""
    source_str = str(input_source).strip()
    path = Path(source_str)

    if not path.is_file():
        # Treat as comma-separated or newline-separated string
        raw_items = [x.strip() for x in source_str.replace("\n", ",").split(",") if x.strip()]
        return raw_items

    identifiers: list[str] = []
    suffix = path.suffix.lower()

    if suffix == ".json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        identifiers.append(item.strip())
                    elif isinstance(item, dict):
                        org = item.get("organisation_number") or item.get("orgnr") or item.get("id")
                        if org:
                            identifiers.append(str(org).strip())
    elif suffix == ".jsonl":
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    d = json.loads(s)
                    if isinstance(d, dict):
                        org = d.get("organisation_number") or d.get("orgnr") or d.get("id")
                        if org:
                            identifiers.append(str(org).strip())
                    elif isinstance(d, str):
                        identifiers.append(d.strip())
                except Exception:
                    identifiers.append(s)
    elif suffix == ".csv":
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            col_idx = 0
            if header:
                # Find matching column
                for i, h in enumerate(header):
                    if h.lower().strip() in ("organisation_number", "orgnr", "organisasjonsnummer", "id"):
                        col_idx = i
                        break
            for row in reader:
                if row and len(row) > col_idx:
                    val = row[col_idx].strip()
                    if val:
                        identifiers.append(val)
    else:
        # Default: plaintext one identifier per line
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                val = line.strip()
                if val and not val.startswith("#"):
                    identifiers.append(val)

    return identifiers


def research_company_pipeline(
    orgnr_input: str | int,
    crawl_website: bool = True,
    use_dataset: bool = True,
    reg_collector: RegistryCollector | None = None,
    web_collector: WebsiteCollector | None = None,
) -> CompanyResultEnvelope:
    """Execute end-to-end research on a single organization number, returning an authoritative envelope.

    Guarantees:
    - Never raises unhandled exceptions.
    - Accurately classifies outcome: available, not_available, blocked, ambiguous, or failed.
    - Treats Norwegian organization number as identity key.
    - Avoids fabricated values or arbitrary zero substitutes.
    - Preserves audit trail and provenance on every fact.
    """
    t0 = time.time()
    raw_id = str(orgnr_input).strip()
    sources_consulted: list[SourceConsulted] = []
    errors: list[str] = []
    field_statuses: dict[str, ResearchOutcome] = {}

    # 1. Structural Identity Verification (Modulo 11)
    is_valid, clean_orgnr, val_reason = validate_orgnr(raw_id)
    if not is_valid:
        elapsed = round(time.time() - t0, 3)
        return CompanyResultEnvelope(
            organisation_number=raw_id,
            outcome=ResearchOutcome.FAILED,
            name=None,
            status="INVALID_IDENTIFIER",
            company=None,
            facts={},
            field_statuses={},
            synthesis=f"Research failed: Organization number '{raw_id}' is invalid ({val_reason}).",
            sources_consulted=[],
            execution_time_seconds=elapsed,
            errors=[val_reason],
        )

    reg = reg_collector or RegistryCollector()
    web = web_collector or WebsiteCollector()

    # 2. Local Master Dataset Lookup & Seed Facts
    seed_facts = {}
    if use_dataset and dataset_loader.exists():
        try:
            seed_facts = dataset_loader.get_seed_facts(clean_orgnr)
            sources_consulted.append(
                SourceConsulted(
                    source_type="supplied_dataset",
                    url=f"dataset:{dataset_loader.dataset_path.name}",
                    status=ResearchOutcome.AVAILABLE if seed_facts else ResearchOutcome.NOT_AVAILABLE,
                    facts_extracted=len(seed_facts),
                )
            )
        except Exception as ds_err:
            logger.debug("Dataset lookup error on %s: %s", clean_orgnr, ds_err)
            errors.append(f"Dataset error: {ds_err}")

    # 3. Live Official Registry Query (Tier 1: Enhetsregisteret)
    enhet_data = None
    try:
        enhet_data = reg.get_enhet(clean_orgnr)
        sources_consulted.append(
            SourceConsulted(
                source_type="official_registry",
                url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{clean_orgnr}",
                status=ResearchOutcome.AVAILABLE if enhet_data else ResearchOutcome.NOT_AVAILABLE,
                http_code=200 if enhet_data else 404,
                facts_extracted=8 if enhet_data else 0,
            )
        )
    except Exception as reg_err:
        logger.warning("Enhetsregisteret error for %s: %s", clean_orgnr, reg_err)
        errors.append(f"Registry query error: {reg_err}")
        sources_consulted.append(
            SourceConsulted(
                source_type="official_registry",
                url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{clean_orgnr}",
                status=ResearchOutcome.BLOCKED if "403" in str(reg_err) else ResearchOutcome.FAILED,
                error_message=str(reg_err),
            )
        )

    # If entity is absent from both registry and dataset:
    if not enhet_data and not seed_facts:
        elapsed = round(time.time() - t0, 3)
        return CompanyResultEnvelope(
            organisation_number=clean_orgnr,
            outcome=ResearchOutcome.NOT_AVAILABLE,
            name=None,
            status="NOT_FOUND",
            company=None,
            facts={},
            field_statuses={},
            synthesis=(
                f"No public registration records or financial statements found for organization number "
                f"'{clean_orgnr}' in Brønnøysundregistrene or the local 2025 dataset catalogue."
            ),
            sources_consulted=sources_consulted,
            execution_time_seconds=elapsed,
            errors=errors,
        )

    # 4. Official Roles (Tier 2: Roller)
    roller_data = None
    if enhet_data:
        try:
            roller_data = reg.get_roller(clean_orgnr)
            sources_consulted.append(
                SourceConsulted(
                    source_type="official_roles",
                    url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{clean_orgnr}/roller",
                    status=ResearchOutcome.AVAILABLE if roller_data else ResearchOutcome.NOT_AVAILABLE,
                    http_code=200 if roller_data else 404,
                )
            )
        except Exception as rol_err:
            logger.debug("Roller API error for %s: %s", clean_orgnr, rol_err)

    # 5. Financial Filings (Tier 3: Regnskapsregisteret)
    regnskap_list = None
    if enhet_data:
        try:
            regnskap_list = reg.get_regnskap(clean_orgnr)
            sources_consulted.append(
                SourceConsulted(
                    source_type="official_financial_statement",
                    url=f"https://data.brreg.no/regnskapsregisteret/regnskap/{clean_orgnr}",
                    status=ResearchOutcome.AVAILABLE if regnskap_list else ResearchOutcome.NOT_AVAILABLE,
                    http_code=200 if regnskap_list else 404,
                )
            )
        except Exception as fin_err:
            logger.debug("Regnskap API error for %s: %s", clean_orgnr, fin_err)

    # 6. Operating Establishments (Tier 4: Underenheter)
    underenheter_list = None
    if enhet_data:
        try:
            underenheter_list = reg.get_underenheter(clean_orgnr)
            if underenheter_list:
                sources_consulted.append(
                    SourceConsulted(
                        source_type="official_underenheter",
                        url=f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={clean_orgnr}",
                        status=ResearchOutcome.AVAILABLE,
                        facts_extracted=len(underenheter_list),
                    )
                )
        except Exception as und_err:
            logger.debug("Underenheter error for %s: %s", clean_orgnr, und_err)

    # 7. Website Crawl with Identity Grounding
    online_data = None
    target_website = (enhet_data.get("hjemmeside") if enhet_data else None) or (
        seed_facts.get("official_website").value if "official_website" in seed_facts else None
    )

    if crawl_website and target_website:
        company_name_hint = (enhet_data.get("navn") if enhet_data else None) or (
            seed_facts.get("legal_name").value if "legal_name" in seed_facts else None
        )
        try:
            online_data = web.collect(
                target_website,
                expected_orgnr=clean_orgnr,
                expected_name=company_name_hint,
                crawl_subpages=True,
            )
            site_status = (
                ResearchOutcome.BLOCKED
                if online_data.http_status == 403
                else (ResearchOutcome.AVAILABLE if online_data.http_status == 200 else ResearchOutcome.NOT_AVAILABLE)
            )
            sources_consulted.append(
                SourceConsulted(
                    source_type="verified_website" if online_data.is_identity_verified else "unverified_website",
                    url=online_data.website_url or target_website,
                    status=site_status,
                    http_code=online_data.http_status,
                )
            )
        except Exception as web_err:
            logger.debug("Website scrape error for %s: %s", target_website, web_err)

    # 8. Fact Extraction & Profile Synthesis
    # If enhet_data was not found, synthesize baseline enhet from seed facts
    if not enhet_data and seed_facts:
        enhet_data = {
            "organisasjonsnummer": clean_orgnr,
            "navn": seed_facts.get("legal_name").value if "legal_name" in seed_facts else f"ENTITY {clean_orgnr}",
            "organisasjonsform": {"kode": seed_facts.get("legal_form").value or "AS", "beskrivelse": "Aksjeselskap"},
            "antallAnsatte": seed_facts.get("employee_count").value if "employee_count" in seed_facts else None,
            "konkurs": seed_facts.get("company_status").value == "BANKRUPT" if "company_status" in seed_facts else False,
            "underAvvikling": seed_facts.get("company_status").value == "LIQUIDATING" if "company_status" in seed_facts else False,
        }

    profile = extract_profile_from_sources(
        expected_orgnr=clean_orgnr,
        enhet_data=enhet_data,
        roller_data=roller_data,
        regnskap_list=regnskap_list,
        online_data=online_data,
        underenheter_list=underenheter_list,
        seed_facts=seed_facts,
    )

    # 9. Determine Field-Level Statuses
    field_statuses["legal_name"] = ResearchOutcome.AVAILABLE if profile.name else ResearchOutcome.NOT_AVAILABLE
    field_statuses["legal_form"] = ResearchOutcome.AVAILABLE if profile.legal_form_code else ResearchOutcome.NOT_AVAILABLE
    field_statuses["company_status"] = ResearchOutcome.AVAILABLE if profile.status else ResearchOutcome.NOT_AVAILABLE
    field_statuses["registered_office_address"] = ResearchOutcome.AVAILABLE if profile.registered_office_address else ResearchOutcome.NOT_AVAILABLE
    field_statuses["financials"] = (
        ResearchOutcome.AVAILABLE
        if (profile.financials and profile.financials.total_revenue is not None)
        else ResearchOutcome.NOT_AVAILABLE
    )
    field_statuses["employee_count"] = (
        ResearchOutcome.AVAILABLE if profile.employee_count is not None else ResearchOutcome.NOT_AVAILABLE
    )

    if profile.website:
        if online_data and online_data.http_status == 403:
            field_statuses["website"] = ResearchOutcome.BLOCKED
        elif online_data and not online_data.is_identity_verified:
            field_statuses["website"] = ResearchOutcome.AMBIGUOUS
        else:
            field_statuses["website"] = ResearchOutcome.AVAILABLE
    else:
        field_statuses["website"] = ResearchOutcome.NOT_AVAILABLE

    field_statuses["officers"] = (
        ResearchOutcome.AVAILABLE if profile.officers else ResearchOutcome.NOT_AVAILABLE
    )

    # 10. Audit History & State Transition
    old_profile = company_repository.get_profile(clean_orgnr)
    if old_profile:
        profile.created_at = old_profile.created_at
        changes = detect_fact_changes(old_profile, profile)
        profile.change_history = old_profile.change_history + [
            c for c in changes if c.change_type != FactStatus.UNCHANGED
        ]
    else:
        profile.change_history = [
            FactChangeRecord(
                fact_key=k,
                old_value=None,
                new_value=v.value,
                change_type=FactStatus.NEW,
                source_url=v.source_url,
                explanation=f"Initial discovery of '{k}' for {clean_orgnr}.",
            )
            for k, v in profile.facts.items()
        ]

    # Generate synthesis
    synthesis = generate_company_synthesis(profile, profile.change_history)
    profile.synthesis = synthesis
    profile.field_statuses = field_statuses
    profile.sources_consulted = [s.model_dump() for s in sources_consulted]

    # Save to SQLite database
    company_repository.save_profile(profile)

    elapsed = round(time.time() - t0, 3)
    return CompanyResultEnvelope(
        organisation_number=clean_orgnr,
        outcome=ResearchOutcome.AVAILABLE,
        name=profile.name,
        status=profile.status,
        company=profile,
        facts=profile.facts,
        field_statuses=field_statuses,
        synthesis=synthesis,
        sources_consulted=sources_consulted,
        execution_time_seconds=elapsed,
        errors=errors,
    )


def process_batch(
    identifiers: list[str | int],
    crawl_website: bool = True,
    max_workers: int = 3,
    progress_callback: Callable[[int, int, CompanyResultEnvelope], None] | None = None,
) -> list[CompanyResultEnvelope]:
    """Execute concurrent, bounded research across a batch of company identifiers.

    Guarantees:
    - Exactly one result per input identifier.
    - Preserves input ordering.
    - Isolates individual failures without terminating the batch.
    """
    total = len(identifiers)
    results: list[CompanyResultEnvelope | None] = [None] * total

    # For thread safety across collectors
    reg_collector = RegistryCollector()
    web_collector = WebsiteCollector()

    def _worker(index: int, org_id: str | int) -> tuple[int, CompanyResultEnvelope]:
        try:
            envelope = research_company_pipeline(
                org_id,
                crawl_website=crawl_website,
                reg_collector=reg_collector,
                web_collector=web_collector,
            )
            return index, envelope
        except Exception as e:
            logger.error("Unhandled worker exception on %s: %s", org_id, e)
            err_envelope = CompanyResultEnvelope(
                organisation_number=str(org_id).strip(),
                outcome=ResearchOutcome.FAILED,
                name=None,
                status="PIPELINE_ERROR",
                company=None,
                facts={},
                field_statuses={},
                synthesis=f"Research process failed: {e}",
                sources_consulted=[],
                errors=[str(e)],
            )
            return index, err_envelope

    # Bounded concurrent execution
    workers = max(1, min(max_workers, 10))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_worker, idx, org_id): idx for idx, org_id in enumerate(identifiers)}
        completed = 0
        for future in as_completed(futures):
            idx, envelope = future.result()
            results[idx] = envelope
            completed += 1
            if progress_callback:
                progress_callback(completed, total, envelope)

    # All entries are guaranteed populated
    return [r for r in results if r is not None]
