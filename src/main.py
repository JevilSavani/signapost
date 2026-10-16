"""Signalpost — Norwegian Company Intelligence Agent.

Main CLI interface supporting single-company research, 1,000+ dataset building,
intelligent profile refresh, and competition evaluation benchmark.
"""

import argparse
import json
import logging
import sys
from rich.console import Console
from rich.table import Table

from src.collectors.registry import RegistryCollector
from src.collectors.website import WebsiteCollector
from src.config import settings
from src.extraction.company_facts import extract_profile_from_sources
from src.models.company import CompanyProfile
from src.storage.repositories import company_repository
from src.utils.rate_limiter import global_budget
from src.validation.identity import parse_and_verify_orgnr, InvalidOrgnrError

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("signalpost")
console = Console()


def research_company(orgnr_input: str, crawl_website: bool = True) -> CompanyProfile | None:
    """End-to-end research workflow for a single Norwegian organization number."""
    try:
        clean_orgnr = parse_and_verify_orgnr(orgnr_input)
    except InvalidOrgnrError as e:
        logger.error("Validation failed: %s", e)
        raise

    reg_collector = RegistryCollector()
    web_collector = WebsiteCollector()

    logger.info("Fetching Enhetsregisteret official registry data for %s...", clean_orgnr)
    enhet = reg_collector.get_enhet(clean_orgnr)
    if not enhet:
        logger.warning("No registration record found for %s in Brønnøysundregistrene.", clean_orgnr)
        return None

    # Fetch roles (management & board)
    logger.info("Fetching registered roles for %s...", clean_orgnr)
    roller = reg_collector.get_roller(clean_orgnr)

    # Fetch accounting / financial statements
    logger.info("Fetching financial filings for %s...", clean_orgnr)
    regnskap = reg_collector.get_regnskap(clean_orgnr)

    # Crawl website if registered and permitted
    online_data = None
    website_url = enhet.get("hjemmeside")
    if crawl_website and website_url:
        logger.info("Collecting verified public info from website: %s", website_url)
        online_data = web_collector.collect(website_url)

    # Extract verified facts
    profile = extract_profile_from_sources(
        expected_orgnr=clean_orgnr,
        enhet_data=enhet,
        roller_data=roller,
        regnskap_list=regnskap,
        online_data=online_data,
    )

    # Save to SQLite database
    company_repository.save_profile(profile)
    return profile


def display_profile_table(profile: CompanyProfile) -> None:
    """Print rich CLI summary table."""
    table = Table(title=f"Company Profile: {profile.name} ({profile.registration_number})")
    table.add_column("Attribute", style="cyan", no_wrap=True)
    table.add_column("Verified Value", style="white")

    summary = profile.get_summary()
    for k, v in summary.items():
        table.add_row(k.replace("_", " ").title(), str(v))

    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Signalpost — Norwegian Company Intelligence Agent (Builderr Challenge)"
    )
    parser.add_argument(
        "--orgnr",
        type=str,
        help="Norwegian 9-digit company registration number to research",
    )
    parser.add_argument(
        "--format",
        choices=["json", "summary", "text"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--no-website",
        action="store_true",
        help="Skip crawling company website (registry only)",
    )
    parser.add_argument(
        "--build-dataset",
        type=int,
        metavar="COUNT",
        help="Collect and validate COUNT verified company profiles (e.g. 1000)",
    )
    parser.add_argument(
        "--refresh",
        type=str,
        metavar="ORGNR",
        help="Refresh and perform change audit on existing company profile",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Run competition evaluation suite on 100 sample companies",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display agent operational statistics and budget consumption",
    )
    parser.add_argument(
        "--export",
        type=str,
        metavar="PATH",
        help="Export all saved profiles to JSONL",
    )

    args = parser.parse_args()

    if args.status:
        count = company_repository.count_profiles()
        metrics = global_budget.get_summary()
        console.print(f"[bold green]Signalpost Status[/bold green]")
        console.print(f"Stored Profiles: {count}")
        console.print(f"Outbound Requests Made: {metrics['outbound_requests']}/{metrics['max_requests']}")
        console.print(f"Cache Hits: {metrics['cache_hits']}")
        console.print(f"Elapsed Time: {metrics['elapsed_seconds']}s / {metrics['max_seconds']}s")
        console.print(f"Declared API Cost: ${metrics['declared_cost_usd']}")
        return

    if args.export:
        count = company_repository.export_profiles_to_jsonl(args.export)
        console.print(f"[green]Exported {count} profiles to {args.export}[/green]")
        return

    if args.orgnr:
        try:
            profile = research_company(args.orgnr, crawl_website=not args.no_website)
            if not profile:
                console.print(f"[red]Company '{args.orgnr}' not found.[/red]")
                sys.exit(1)

            if args.format == "json":
                print(profile.model_dump_json(indent=2))
            elif args.format == "summary":
                display_profile_table(profile)
            else:
                summary = profile.get_summary()
                for k, v in summary.items():
                    print(f"{k}: {v}")

        except InvalidOrgnrError as err:
            console.print(f"[bold red]Validation Error:[/bold red] {err}")
            sys.exit(2)
        except Exception as exc:
            logger.exception("Execution failed: %s", exc)
            sys.exit(1)
        return

    if args.build_dataset:
        from src.updates.refresh import build_dataset_pipeline

        console.print(f"[bold blue]Initiating dataset build for {args.build_dataset} companies...[/bold blue]")
        stats = build_dataset_pipeline(target_count=args.build_dataset)
        console.print(f"[bold green]Dataset build complete:[/bold green] {stats}")
        return

    if args.refresh:
        from src.updates.refresh import refresh_company_profile

        diff = refresh_company_profile(args.refresh)
        console.print(f"[bold cyan]Refresh Results for {args.refresh}:[/bold cyan]")
        console.print(json.dumps(diff, indent=2))
        return

    if args.evaluate:
        from src.evaluation.metrics import run_evaluation_benchmark

        console.print("[bold yellow]Running Signalpost Competition Evaluation Suite...[/bold yellow]")
        report = run_evaluation_benchmark()
        console.print(f"[bold green]Evaluation Score:[/bold green] {report['total_score']}/100.0")
        console.print(json.dumps(report, indent=2))
        return

    # If no flag provided, show help
    parser.print_help()


if __name__ == "__main__":
    main()
