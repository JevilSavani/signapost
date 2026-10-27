"""Signalpost — Norwegian Company Intelligence Agent.

Main CLI interface supporting single-company research, streaming batch processing,
1,000+ dataset building, intelligent profile refresh, competition evaluation benchmark,
and 100-company pipeline smoke tests.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from rich.console import Console
from rich.table import Table

if sys.version_info >= (3, 7):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config import settings
from src.models.company import CompanyProfile
from src.models.result import CompanyResultEnvelope
from src.pipeline.research import (
    load_input_identifiers,
    process_batch,
    research_company_pipeline,
)
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
    """End-to-end research workflow for a single Norwegian organization number.

    Preserves full backward compatibility with tests and callers expecting a CompanyProfile.
    """
    envelope = research_company_pipeline(orgnr_input, crawl_website=crawl_website)
    return envelope.company


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
        help="Norwegian 9-digit company registration number (or comma-separated list) to research",
    )
    parser.add_argument(
        "--batch",
        "--input",
        dest="input_batch",
        type=str,
        metavar="PATH",
        help="Path to file containing company numbers to research (one orgnr per line TXT, JSON, JSONL, or CSV)",
    )
    parser.add_argument(
        "--output",
        type=str,
        metavar="PATH",
        help="Destination path for research results (JSON Lines .jsonl or JSON .json format)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "jsonl", "envelope", "summary", "text"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        metavar="WORKERS",
        help="Bounded concurrent worker threads for batch processing (default: 3, max: 10)",
    )
    parser.add_argument(
        "--no-website",
        action="store_true",
        help="Skip crawling company website (registry and local dataset only)",
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
        "--smoke-test",
        action="store_true",
        help="Run official 100-company end-to-end pipeline smoke test",
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

    if args.smoke_test:
        from src.evaluation.metrics import run_100_company_smoke_test

        console.print("[bold yellow]Running Signalpost 100-Company Smoke Test...[/bold yellow]")
        report = run_100_company_smoke_test()
        console.print(f"[bold green]Smoke Test Status:[/bold green] {report['status']} ({report['total_results_returned']}/100 results)")
        console.print(json.dumps(report, indent=2))
        return

    if args.evaluate:
        from src.evaluation.metrics import run_evaluation_benchmark

        console.print("[bold yellow]Running Signalpost Competition Evaluation Suite...[/bold yellow]")
        report = run_evaluation_benchmark()
        console.print(f"[bold green]Evaluation Score:[/bold green] {report['total_score']}/100.0")
        console.print(json.dumps(report, indent=2))
        return

    # Batch file research
    if args.input_batch:
        try:
            identifiers = load_input_identifiers(args.input_batch)
        except Exception as exc:
            sys.stderr.write(f"Error: Failed to read batch input file '{args.input_batch}': {exc}\n")
            sys.exit(1)

        workers = max(1, min(args.concurrency, 10))
        envelopes = process_batch(
            identifiers,
            crawl_website=not args.no_website,
            max_workers=workers,
        )

        if args.output:
            out_path = Path(args.output)
            tmp_path = None
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = out_path.with_name(f".{out_path.name}.tmp_{os.getpid()}")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    if out_path.suffix.lower() == ".json":
                        json.dump([e.model_dump(mode="json") for e in envelopes], f, indent=2, ensure_ascii=False)
                    else:
                        for e in envelopes:
                            f.write(e.model_dump_json() + "\n")
                # Atomic replacement
                tmp_path.replace(out_path)
            except Exception as write_err:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                sys.stderr.write(f"Error: Failed to write output file '{args.output}': {write_err}\n")
                sys.exit(1)

            sys.stderr.write(f"Batch completed: Saved {len(envelopes)} results to {args.output}\n")
        else:
            if args.format == "json" or args.format == "envelope":
                print(json.dumps([e.model_dump(mode="json") for e in envelopes], indent=2, ensure_ascii=False))
            elif args.format == "jsonl":
                for e in envelopes:
                    print(e.model_dump_json())
            elif args.format == "summary" or args.format == "text":
                for e in envelopes:
                    status_indicator = "[green]AVAILABLE[/green]" if e.outcome.value == "available" else f"[yellow]{e.outcome.value.upper()}[/yellow]"
                    console.print(f"{status_indicator} {e.organisation_number}: {e.name or 'N/A'}")
            else:
                for e in envelopes:
                    print(e.model_dump_json())
        return

    # Single or comma-separated orgnr research
    if args.orgnr:
        org_list = [x.strip() for x in args.orgnr.split(",") if x.strip()]
        if len(org_list) > 1:
            envelopes = process_batch(org_list, crawl_website=not args.no_website)
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump([e.model_dump(mode="json") for e in envelopes], f, indent=2, ensure_ascii=False)
            print(json.dumps([e.model_dump(mode="json") for e in envelopes], indent=2, ensure_ascii=False))
            return

        target_org = org_list[0]
        try:
            envelope = research_company_pipeline(target_org, crawl_website=not args.no_website)

            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(envelope.model_dump_json(indent=2))

            if args.format == "json" or args.format == "envelope":
                print(envelope.model_dump_json(indent=2))
            elif args.format == "summary":
                if envelope.company:
                    display_profile_table(envelope.company)
                else:
                    console.print(f"[yellow]Status:[/yellow] {envelope.outcome.value}")
                    console.print(f"[bold]{envelope.synthesis}[/bold]")
            else:
                if envelope.synthesis:
                    print(envelope.synthesis)
                elif envelope.company:
                    for k, v in envelope.company.get_summary().items():
                        print(f"{k}: {v}")
                else:
                    print(f"Outcome: {envelope.outcome.value}")

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

    # If no flag provided, show help
    parser.print_help()


if __name__ == "__main__":
    main()
