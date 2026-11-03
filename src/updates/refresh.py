"""Intelligent profile refresh, change detection, and large-scale dataset pipeline."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from src.collectors.registry import RegistryCollector
from src.collectors.website import WebsiteCollector
from src.config import settings
from src.extraction.company_facts import extract_profile_from_sources
from src.models.company import CompanyProfile
from src.models.evidence import FactCategory, FactChangeRecord, FactStatus
from src.storage.dataset_loader import dataset_loader
from src.storage.repositories import company_repository
from src.utils.rate_limiter import global_budget
from src.validation.identity import parse_and_verify_orgnr, validate_orgnr

logger = logging.getLogger(__name__)


def detect_fact_changes(
    old_profile: CompanyProfile,
    new_profile: CompanyProfile,
    preserve_missing_facts: bool = False,
) -> list[FactChangeRecord]:
    """Compare two profiles fact-by-fact and compute semantic change records.

    Rules:
    - Never overwrite verified facts with unsupported guesses.
    - Preserve change history in an immutable audit trail.
    - Detect NEW, CHANGED, UNCHANGED, CONFLICTING, and UNAVAILABLE facts.
    """
    now_utc = datetime.now(timezone.utc)
    changes: list[FactChangeRecord] = []

    old_keys = set(old_profile.facts.keys())
    new_keys = set(new_profile.facts.keys())

    # Detect new facts
    for key in new_keys - old_keys:
        new_fact = new_profile.facts[key]
        changes.append(
            FactChangeRecord(
                fact_key=key,
                old_value=None,
                new_value=new_fact.value,
                change_type=FactStatus.NEW,
                detected_at=now_utc,
                source_url=new_fact.source_url,
                explanation=f"New fact '{key}' discovered from {new_fact.source_type} ({new_fact.source_url}).",
            )
        )

    # Detect changed, conflicting, or unchanged facts
    for key in old_keys & new_keys:
        old_fact = old_profile.facts[key]
        new_fact = new_profile.facts[key]

        if old_fact.value != new_fact.value:
            # 1. Financial protection rules
            if old_fact.category == FactCategory.FINANCIAL or key in (
                "total_revenue", "operating_revenue", "operating_profit", "net_income",
                "total_assets", "total_equity", "total_liabilities", "is_audited"
            ):
                old_end = getattr(old_fact, "reporting_period_end", None) or ""
                new_end = getattr(new_fact, "reporting_period_end", None) or ""
                old_snip = getattr(old_fact, "evidence_snippet", "") or ""
                new_snip = getattr(new_fact, "evidence_snippet", "") or ""

                # Rule A: Never overwrite with older reporting period
                if old_end and new_end and new_end < old_end:
                    new_profile.facts[key] = old_fact
                    changes.append(
                        FactChangeRecord(
                            fact_key=key,
                            old_value=old_fact.value,
                            new_value=new_fact.value,
                            change_type=FactStatus.CONFLICTING,
                            detected_at=now_utc,
                            source_url=new_fact.source_url,
                            explanation=(
                                f"Rejected financial overwrite for '{key}': incoming filing period ({new_end}) "
                                f"is older than verified period ({old_end}). Preserving verified figure."
                            ),
                        )
                    )
                    continue

                # Rule B: Never overwrite group consolidated figures (KONSERN) with company parent-only (SELSKAP)
                is_group = bool(
                    getattr(new_profile, "is_part_of_group", False)
                    or getattr(old_profile, "is_part_of_group", False)
                )
                if is_group and "KONSERN" in old_snip and "SELSKAP" in new_snip:
                    new_profile.facts[key] = old_fact
                    changes.append(
                        FactChangeRecord(
                            fact_key=key,
                            old_value=old_fact.value,
                            new_value=new_fact.value,
                            change_type=FactStatus.CONFLICTING,
                            detected_at=now_utc,
                            source_url=new_fact.source_url,
                            explanation=(
                                f"Rejected financial overwrite for '{key}': incoming statement scope is parent-only (SELSKAP) "
                                f"while company is part of a group with verified consolidated figures (KONSERN). Preserving verified figure."
                            ),
                        )
                    )
                    continue

            # 2. Employee count verification rules
            if key == "employee_count":
                if old_fact.source_type == "official_registry" and new_fact.source_type not in (
                    "official_registry", "official_financial_statement"
                ):
                    new_profile.facts[key] = old_fact
                    changes.append(
                        FactChangeRecord(
                            fact_key=key,
                            old_value=old_fact.value,
                            new_value=new_fact.value,
                            change_type=FactStatus.CONFLICTING,
                            detected_at=now_utc,
                            source_url=new_fact.source_url,
                            explanation=(
                                f"Rejected ungrounded employee count change: incoming source ({new_fact.source_type}) "
                                f"lacks official registry authority compared to verified registry value ({old_fact.value}). Retaining verified count."
                            ),
                        )
                    )
                    continue

            # Legitimate change
            change_type = FactStatus.CHANGED
            explanation = (
                f"Fact '{key}' updated from '{old_fact.value}' to '{new_fact.value}' "
                f"via {new_fact.source_url}."
            )
            changes.append(
                FactChangeRecord(
                    fact_key=key,
                    old_value=old_fact.value,
                    new_value=new_fact.value,
                    change_type=change_type,
                    detected_at=now_utc,
                    source_url=new_fact.source_url,
                    explanation=explanation,
                )
            )
        else:
            changes.append(
                FactChangeRecord(
                    fact_key=key,
                    old_value=old_fact.value,
                    new_value=new_fact.value,
                    change_type=FactStatus.UNCHANGED,
                    detected_at=now_utc,
                    source_url=new_fact.source_url,
                    explanation=f"Fact '{key}' verified unchanged via {new_fact.source_url}.",
                )
            )

    # Detect facts unavailable in the new scrape & preserve verified historical values
    for key in old_keys - new_keys:
        old_fact = old_profile.facts[key]
        if preserve_missing_facts:
            new_profile.facts[key] = old_fact

        # Check if source failed vs omitted
        source_failed = False
        if hasattr(new_profile, "sources_consulted") and new_profile.sources_consulted:
            for sc in new_profile.sources_consulted:
                sc_url = sc.get("url") if isinstance(sc, dict) else getattr(sc, "url", "")
                sc_status = sc.get("status") if isinstance(sc, dict) else getattr(sc, "status", None)
                if sc_url == old_fact.source_url:
                    st_val = sc_status.value if hasattr(sc_status, "value") else str(sc_status)
                    if st_val in ("failed", "blocked"):
                        source_failed = True
                        break

        change_type = FactStatus.FAILED_REFRESH if source_failed else FactStatus.UNAVAILABLE
        explanation = (
            f"Source fetch failed for '{key}' ({old_fact.source_url}). Preserving prior verified value '{old_fact.value}'."
            if source_failed
            else f"Fact '{key}' was not observed in current fetch. Preserving prior verified value in historical audit trail."
        )

        changes.append(
            FactChangeRecord(
                fact_key=key,
                old_value=old_fact.value,
                new_value=None,
                change_type=change_type,
                detected_at=now_utc,
                source_url=old_fact.source_url,
                explanation=explanation,
            )
        )

    # Preserve top-level fields on new_profile if currently omitted and preservation requested
    if preserve_missing_facts:
        if old_profile.financials and not new_profile.financials:
            new_profile.financials = old_profile.financials
        if old_profile.employee_count is not None and new_profile.employee_count is None:
            new_profile.employee_count = old_profile.employee_count
        if old_profile.website and not new_profile.website:
            new_profile.website = old_profile.website

    return changes


def refresh_company_profile(orgnr: str, crawl_website: bool = True) -> dict[str, Any]:
    """Refresh an existing profile, detect changes, update database, and log audit trail."""
    clean_orgnr = parse_and_verify_orgnr(orgnr)
    old_profile = company_repository.get_profile(clean_orgnr)

    reg_collector = RegistryCollector()
    web_collector = WebsiteCollector()

    enhet = reg_collector.get_enhet(clean_orgnr)
    if not enhet:
        raise ValueError(f"Company {clean_orgnr} not found in registry during refresh")

    roller = reg_collector.get_roller(clean_orgnr)
    regnskap = reg_collector.get_regnskap(clean_orgnr)

    online_data = None
    website_url = enhet.get("hjemmeside")
    if crawl_website and website_url:
        online_data = web_collector.collect(website_url)

    from src.storage.dataset_loader import dataset_loader
    from src.extraction.synthesis import generate_company_synthesis

    seed_facts = None
    if dataset_loader.exists():
        try:
            seed_facts = dataset_loader.get_seed_facts(clean_orgnr)
        except Exception:
            pass

    new_profile = extract_profile_from_sources(
        expected_orgnr=clean_orgnr,
        enhet_data=enhet,
        roller_data=roller,
        regnskap_list=regnskap,
        online_data=online_data,
        seed_facts=seed_facts,
    )

    if old_profile:
        # Preserve creation timestamp
        new_profile.created_at = old_profile.created_at

        # Compute change delta and preserve prior facts
        changes = detect_fact_changes(old_profile, new_profile, preserve_missing_facts=True)
        # Combine existing history with new changes
        new_profile.change_history = old_profile.change_history + [
            c for c in changes if c.change_type != FactStatus.UNCHANGED
        ]
    else:
        changes = [
            FactChangeRecord(
                fact_key=k,
                old_value=None,
                new_value=v.value,
                change_type=FactStatus.NEW,
                source_url=v.source_url,
                explanation=f"Initial profile creation for {clean_orgnr}.",
            )
            for k, v in new_profile.facts.items()
        ]
        new_profile.change_history = changes

    new_profile.synthesis = generate_company_synthesis(new_profile, new_profile.change_history)
    company_repository.save_profile(new_profile)

    return {
        "orgnr": clean_orgnr,
        "name": new_profile.name,
        "total_facts": len(new_profile.facts),
        "changes_detected": len([c for c in changes if c.change_type == FactStatus.CHANGED]),
        "new_facts": len([c for c in changes if c.change_type == FactStatus.NEW]),
        "unchanged_facts": len([c for c in changes if c.change_type == FactStatus.UNCHANGED]),
        "audit_entries_added": len([c for c in changes if c.change_type != FactStatus.UNCHANGED]),
        "details": [c.model_dump(mode="json") for c in changes],
    }


def build_dataset_pipeline(
    target_count: int = 1000,
    page_size: int = 100,
    organisasjonsform: str = "AS",
    use_master_dataset: bool = True,
) -> dict[str, Any]:
    """Execute reproducible large-scale collection to build at least 1,000 verified profiles.

    - Prioritizes streaming from local master catalogue (data/financial-filer-master-2025.jsonl)
    - Seamlessly falls back to official Brønnøysundregistrene Enhetsregisteret bulk query API
    - Strictly validates all 9-digit registration numbers with Modulo 11
    - Deduplicates records
    - Stores complete profiles in SQLite
    - Exports to JSONL
    - Produces a comprehensive data quality report
    """
    reg_collector = RegistryCollector()
    web_collector = WebsiteCollector()

    attempted = 0
    successfully_processed = 0
    partially_processed = 0
    rejected = 0
    rejection_reasons: dict[str, int] = {}

    seen_orgs: set[str] = set()

    # Determine collection strategy
    stream_from_master = use_master_dataset and dataset_loader.exists()

    if stream_from_master:
        logger.info(
            "Starting dataset collection pipeline using master catalogue: %s (target=%d, form=%s)",
            dataset_loader.dataset_path.name,
            target_count,
            organisasjonsform,
        )
    else:
        logger.info(
            "Starting dataset collection pipeline via live registry API: target=%d, form=%s",
            target_count,
            organisasjonsform,
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
    ) as progress:
        task_label = (
            f"Collecting profiles from {dataset_loader.dataset_path.name}..."
            if stream_from_master
            else "Collecting profiles from registry..."
        )
        task = progress.add_task(task_label, total=target_count)

        if stream_from_master:
            for record in dataset_loader.iter_records():
                attempted += 1
                raw_orgnr = record.organisation_number
                if not raw_orgnr:
                    continue

                if organisasjonsform and record.legal_form and record.legal_form.upper() != organisasjonsform.upper():
                    continue

                is_valid, clean_orgnr, reason = validate_orgnr(raw_orgnr)
                if not is_valid:
                    rejected += 1
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                    continue

                if clean_orgnr in seen_orgs:
                    continue
                seen_orgs.add(clean_orgnr)

                seed_facts = dataset_loader.get_seed_facts(clean_orgnr)
                enhet_payload = {
                    "organisasjonsnummer": clean_orgnr,
                    "navn": record.name or f"ENTITY {clean_orgnr}",
                    "organisasjonsform": {"kode": record.legal_form or "AS", "beskrivelse": "Aksjeselskap"},
                    "antallAnsatte": record.employees,
                    "konkurs": bool(record.bankrupt),
                    "underAvvikling": bool(record.liquidating),
                }

                # For subset with official websites, collect online presence if budget allows
                online_data = None
                website_url = record.website
                if (
                    website_url
                    and successfully_processed < 50
                    and global_budget.remaining_requests > 500
                ):
                    try:
                        online_data = web_collector.collect(website_url)
                    except Exception:
                        pass

                try:
                    profile = extract_profile_from_sources(
                        expected_orgnr=clean_orgnr,
                        enhet_data=enhet_payload,
                        roller_data=None,
                        regnskap_list=None,
                        online_data=online_data,
                        seed_facts=seed_facts,
                    )
                    company_repository.save_profile(profile)
                    successfully_processed += 1
                    progress.update(task, advance=1)

                    if len(profile.facts) < 5:
                        partially_processed += 1
                except Exception as exc:
                    rejected += 1
                    rejection_reasons[str(exc)] = rejection_reasons.get(str(exc), 0) + 1

                if successfully_processed >= target_count:
                    break
        else:
            page = 0
            max_pages = (target_count // page_size) + 4
            while successfully_processed < target_count and page < max_pages:
                try:
                    enheter = reg_collector.list_companies(
                        organisasjonsform=organisasjonsform, page=page, size=page_size
                    )
                    if not enheter:
                        logger.info("No more companies returned at page %d", page)
                        break
                except Exception as e:
                    logger.error("Failed to query page %d: %s", page, e)
                    break

                for enhet in enheter:
                    attempted += 1
                    raw_orgnr = str(enhet.get("organisasjonsnummer", "")).strip()

                    # Validate identity
                    is_valid, clean_orgnr, reason = validate_orgnr(raw_orgnr)
                    if not is_valid:
                        rejected += 1
                        rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                        continue

                    if clean_orgnr in seen_orgs:
                        # Deduplicate
                        continue
                    seen_orgs.add(clean_orgnr)

                    # For selected subset with official websites, collect online presence
                    online_data = None
                    website_url = enhet.get("hjemmeside")
                    if (
                        website_url
                        and successfully_processed < 50
                        and global_budget.remaining_requests > 500
                    ):
                        try:
                            online_data = web_collector.collect(website_url)
                        except Exception:
                            pass

                    seed_facts = None
                    if dataset_loader.exists():
                        try:
                            seed_facts = dataset_loader.get_seed_facts(clean_orgnr)
                        except Exception:
                            pass

                    try:
                        profile = extract_profile_from_sources(
                            expected_orgnr=clean_orgnr,
                            enhet_data=enhet,
                            roller_data=None,
                            regnskap_list=None,
                            online_data=online_data,
                            seed_facts=seed_facts,
                        )
                        company_repository.save_profile(profile)
                        successfully_processed += 1
                        progress.update(task, advance=1)

                        if len(profile.facts) < 5:
                            partially_processed += 1

                    except Exception as exc:
                        rejected += 1
                        rejection_reasons[str(exc)] = rejection_reasons.get(str(exc), 0) + 1

                    if successfully_processed >= target_count:
                        break

                page += 1

    # Export all saved profiles to JSONL
    export_count = company_repository.export_profiles_to_jsonl()

    # Generate dataset quality report
    profiles = company_repository.list_profiles(limit=target_count + 200)
    fact_key_counts: dict[str, int] = {}
    for p in profiles:
        for k in p.facts.keys():
            fact_key_counts[k] = fact_key_counts.get(k, 0) + 1

    total_stored = len(profiles)
    coverage_report = {
        k: f"{(count / total_stored) * 100:.1f}% ({count}/{total_stored})"
        for k, count in sorted(fact_key_counts.items(), key=lambda x: x[1], reverse=True)
    }

    quality_report = {
        "dataset_name": "Signalpost Norwegian Company Intelligence Dataset",
        "source_catalogue": dataset_loader.dataset_path.name if stream_from_master else "Brønnøysundregistrene Enhetsregisteret API",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_count": target_count,
        "total_attempted": attempted,
        "successfully_processed": successfully_processed,
        "partially_processed": partially_processed,
        "rejected_count": rejected,
        "rejection_reasons": rejection_reasons,
        "total_profiles_in_database": total_stored,
        "total_profiles_exported_jsonl": export_count,
        "export_filepath": str(settings.profiles_export_path),
        "database_filepath": str(settings.database_path),
        "fact_coverage": coverage_report,
        "resource_consumption": global_budget.get_summary(),
    }

    # Save quality report to reports/
    report_path = settings.reports_dir / "dataset_quality_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(quality_report, f, indent=2)

    logger.info("Saved dataset quality report to %s", report_path)
    return quality_report
