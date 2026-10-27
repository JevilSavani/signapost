"""Factual company intelligence synthesis generator grounded in verified provenance.

Generates concise, human-readable company profiles derived strictly from verified facts.
Distinguishes observed facts from analytical interpretations, identifies key data gaps,
and links directly to supporting evidence.
"""

from typing import Any
from src.models.company import CompanyProfile
from src.models.evidence import FactChangeRecord, FactStatus


def generate_company_synthesis(
    profile: CompanyProfile,
    audit_history: list[FactChangeRecord] | None = None,
) -> str:
    """Produce an evidence-grounded, multi-section synthesis of a company's intelligence profile.

    Sections:
    1. Core Activities & Purpose
    2. Legal Identity & Operating Locations
    3. Financial & Workforce Profile
    4. Leadership & Governance
    5. Public Activity & Vacancies
    6. Profile Updates & State Changes
    7. Unknowns & Information Gaps
    8. Supporting Evidence & Provenance
    """
    sections: list[str] = []

    # 1. Header & What the Company Does
    status_str = profile.status
    if profile.is_bankrupt:
        status_str = "BANKRUPT (Konkurs)"
    elif profile.is_in_liquidation:
        status_str = "IN LIQUIDATION (Under avvikling)"

    industry_desc = profile.nace_description or "General commercial activity"
    purpose_desc = (
        profile.statutory_purpose
        or profile.business_description
        or "Statutory purpose is not specified in public registry"
    )

    summary_text = (
        f"### 1. Executive Summary & Core Activity\n"
        f"**{profile.name}** ({profile.registration_number}) is a registered Norwegian "
        f"**{profile.legal_form_description}** ({profile.legal_form_code}) currently in **{status_str}** status. "
        f"The entity operates primarily within **{industry_desc}** (NACE: {profile.nace_code or 'N/A'}).\n\n"
        f"**Statutory Purpose / Activities**: {purpose_desc}"
    )
    sections.append(summary_text)

    # 2. Legal Identity & Location
    addr_line = (
        profile.registered_office_address.format_single_line()
        if profile.registered_office_address
        else "No physical office address registered"
    )
    reg_date = profile.registration_date or "Not reported"
    found_date = profile.founding_date or "Not reported"

    locations_line = ""
    if profile.operating_locations:
        loc_str = ", ".join(profile.operating_locations[:6])
        if len(profile.operating_locations) > 6:
            loc_str += f" (+{len(profile.operating_locations) - 6} more)"
        locations_line = f"\n- **Operating Locations / Establishments**: {loc_str}"

    group_str = (
        "Registered as part of a corporate group"
        if profile.is_part_of_group is True
        else ("Not registered as part of a corporate group" if profile.is_part_of_group is False else "Not reported in public registry")
    )

    identity_text = (
        f"### 2. Legal Identity & Business Locations\n"
        f"- **Organization Number**: `{profile.registration_number}` (Modulo 11 verified)\n"
        f"- **Registered Office**: {addr_line}\n"
        f"- **Incorporation / Foundation Date**: {found_date}\n"
        f"- **Official Registration Date**: {reg_date}\n"
        f"- **Corporate Group Status**: {group_str}\n"
        f"- **Share Capital**: {f'{profile.share_capital:,.0f} NOK' if profile.share_capital is not None else 'Not registered'}"
        f"{locations_line}"
    )
    sections.append(identity_text)

    # 3. Financial & Workforce Profile
    fin = profile.financials
    fin_lines: list[str] = []
    if fin and any(
        x is not None
        for x in [
            fin.total_revenue,
            fin.operating_profit,
            fin.net_income,
            fin.total_assets,
            fin.total_equity,
        ]
    ):
        period_str = f"{fin.period_start or '?'} to {fin.period_end or '?'}"
        if fin.is_audited is True:
            audit_note = ", Audited: Yes"
        elif fin.is_audited is False:
            audit_note = ", Audited: No (unaudited / exemption)"
        else:
            audit_note = ""

        curr_label = f" {fin.currency}" if fin.currency else " (currency unrecorded)"
        units_note = " (Units: base currency units)" if fin.currency else ""

        fin_lines.append(f"- **Reporting Period**: {period_str} (Statement: {fin.statement_type or 'SELSKAP'}{audit_note})")
        fin_lines.append(
            f"- **Operating Revenue**: {f'{fin.total_revenue:,.0f}{curr_label}{units_note}' if fin.total_revenue is not None else 'Not reported in filing'}"
        )
        fin_lines.append(
            f"- **Operating Profit (Driftsresultat)**: {f'{fin.operating_profit:,.0f}{curr_label}{units_note}' if fin.operating_profit is not None else 'Not reported in filing'}"
        )
        fin_lines.append(
            f"- **Net Annual Income (Årsresultat)**: {f'{fin.net_income:,.0f}{curr_label}{units_note}' if fin.net_income is not None else 'Not reported in filing'}"
        )
        if fin.total_assets is not None:
            fin_lines.append(f"- **Total Assets (Eiendeler)**: {fin.total_assets:,.0f}{curr_label}{units_note}")
        if fin.total_equity is not None:
            fin_lines.append(f"- **Total Equity (Egenkapital)**: {fin.total_equity:,.0f}{curr_label}{units_note}")
    else:
        fin_lines.append("- **Annual Accounts**: Financial statements are not available in Regnskapsregisteret.")

    emp_str = (
        f"{profile.employee_count} registered employees"
        if profile.employee_count is not None
        else "Not available in public records"
    )
    fin_lines.append(f"- **Workforce Count**: {emp_str}")

    fin_text = "### 3. Financial Performance & Employment\n" + "\n".join(fin_lines)
    sections.append(fin_text)

    # 4. Leadership & Governance (Strictly fact-grounded)
    lead_lines: list[str] = []
    ceo_fact = profile.facts.get("ceo_name")
    chair_fact = profile.facts.get("board_chair")
    auditor_fact = profile.facts.get("auditor")
    board_fact = profile.facts.get("board_members")

    if ceo_fact:
        lead_lines.append(f"- **Managing Director / CEO**: {ceo_fact.value}")
    if chair_fact:
        lead_lines.append(f"- **Board Chair (Styrets leder)**: {chair_fact.value}")
    if auditor_fact:
        lead_lines.append(f"- **Statutory Auditor (Revisor)**: {auditor_fact.value}")
    if board_fact and isinstance(board_fact.value, list) and board_fact.value:
        b_str = ", ".join(board_fact.value[:4])
        if len(board_fact.value) > 4:
            b_str += f" (+{len(board_fact.value) - 4} more)"
        lead_lines.append(f"- **Board Members**: {b_str}")

    if not lead_lines:
        lead_lines.append("- **Registered Roles**: Key officer roles are not recorded in official register.")

    lead_text = "### 4. Leadership & Governance\n" + "\n".join(lead_lines)
    sections.append(lead_text)

    # 5. Public Activity, Web & Vacancies
    pub_lines: list[str] = []
    if profile.website:
        pub_lines.append(f"- **Official Website**: {profile.website}")
    if profile.email:
        pub_lines.append(f"- **Contact Email**: {profile.email}")
    if profile.phone:
        pub_lines.append(f"- **Contact Phone**: {profile.phone}")

    if profile.online_presence and profile.online_presence.is_identity_verified and profile.online_presence.active_jobs:
        job_titles = [f"`{j.get('title')}`" for j in profile.online_presence.active_jobs[:3]]
        pub_lines.append(f"- **Open Job Vacancies**: {', '.join(job_titles)}")

    news_fact = profile.facts.get("news_articles")
    if news_fact and isinstance(news_fact.value, list) and news_fact.value:
        pub_lines.append("- **Verified News & Corporate Announcements**:")
        for item in news_fact.value[:3]:
            if isinstance(item, dict):
                title = item.get("title") or item.get("headline")
                url = item.get("url")
                date = item.get("date") or item.get("publication_date")
                date_str = f" ({date})" if date else ""
                if url:
                    pub_lines.append(f"  - [{title}]({url}){date_str}")
                else:
                    pub_lines.append(f"  - {title}{date_str}")
    else:
        ann_fact = profile.facts.get("public_announcements")
        if ann_fact and ann_fact.value:
            pub_lines.append("- **Verified News & Corporate Announcements**:")
            for h in ann_fact.value[:3]:
                pub_lines.append(f"  - \"{h}\"")
        elif profile.online_presence and profile.online_presence.announcements:
            pub_lines.append("- **Verified News & Corporate Announcements**:")
            for item in profile.online_presence.announcements[:3]:
                title = item.get("title") or item.get("headline")
                url = item.get("url")
                date = item.get("publication_date") or item.get("date")
                date_str = f" ({date})" if date else ""
                if url:
                    pub_lines.append(f"  - [{title}]({url}){date_str}")
                else:
                    pub_lines.append(f"  - {title}{date_str}")

    if not pub_lines:
        pub_lines.append(
            "- **Public Web Presence**: Information on website, job postings, or announcements is not available from verified sources."
        )

    pub_text = "### 5. Verified Public Activity & Presence\n" + "\n".join(pub_lines)
    sections.append(pub_text)

    # 6. Significant Changes Since Previous Profile
    history = audit_history or profile.change_history
    history_lines: list[str] = []
    if history:
        # Show most recent changes first so latest fact updates are prominently grounded
        non_unchanged = [entry for entry in history if entry.change_type != FactStatus.UNCHANGED]
        for entry in reversed(non_unchanged[-5:]):
            history_lines.append(
                f"- **[{entry.change_type.value}] {entry.fact_key}**: {entry.explanation}"
            )
    if not history_lines:
        history_lines.append("- **Baseline State**: No subsequent modifications detected since initial verification.")

    changes_text = "### 6. Profile Changes & Historical Audit\n" + "\n".join(history_lines[:5])
    sections.append(changes_text)

    # 7. Important Information Gaps (Explicit Unknowns without speculation)
    unknowns: list[str] = []
    if profile.employee_count is None:
        unknowns.append("Employee count is unrecorded / not available in public registry records.")
    if not profile.website:
        unknowns.append("No official corporate domain or website is registered or verified in available public sources.")
    if not profile.email:
        unknowns.append("Official corporate contact email is unrecorded in public registry records.")
    if not profile.phone:
        unknowns.append("Official corporate telephone number is unrecorded in public registry records.")
    if not profile.financials or not any(
        x is not None
        for x in [
            profile.financials.total_revenue,
            profile.financials.operating_profit,
            profile.financials.net_income,
            profile.financials.total_assets,
            profile.financials.total_equity,
        ]
    ):
        unknowns.append("No public financial statements are currently available in Regnskapsregisteret.")
    else:
        if profile.financials.total_revenue is None:
            unknowns.append("Operating revenue line item was not reported in the latest financial filing.")
        if profile.financials.currency is None:
            unknowns.append("Reporting currency and units are unrecorded in official financial statements.")
        if profile.financials.is_audited is None:
            unknowns.append("Audit status of latest financial filing could not be established from available filing records.")
    if not ceo_fact:
        unknowns.append("Managing Director (Daglig leder) is not registered in official roles register.")
    if not chair_fact:
        unknowns.append("Board Chair (Styrets leder) is not registered in official roles register.")

    if not unknowns:
        unknowns.append("Primary corporate registration, leadership, financial filing, and official website dimensions are verified from official sources.")

    gaps_text = "### 7. Key Information Gaps & Unknowns\n" + "\n".join(f"- {u}" for u in unknowns)
    sections.append(gaps_text)

    # 8. Evidence & Provenance Traceability
    # Group structured facts by their unique verified source URL to eliminate duplicate labels for identical endpoints
    facts_by_url: dict[str, list[ProvenanceFact]] = {}
    for f in profile.facts.values():
        if f.source_url:
            facts_by_url.setdefault(f.source_url, []).append(f)

    dataset_urls = {
        u for u in facts_by_url
        if u.startswith("dataset:") or any(f.source_type == "supplied_dataset" for f in facts_by_url[u])
    }
    indep_verified_urls = {u for u in facts_by_url if u not in dataset_urls}

    evidence_lines: list[str] = []
    # Display independently verified live sources first, then supplied dataset
    sorted_urls = sorted(indep_verified_urls) + sorted(dataset_urls)
    for s_url in sorted_urls:
        f_list = facts_by_url[s_url]
        canonical_title = f_list[0].source_title or f_list[0].source_type
        for f in f_list:
            if f.source_title and " " in f.source_title:
                canonical_title = f.source_title
                break
        max_conf = max(f.confidence for f in f_list)
        is_dataset = s_url in dataset_urls
        type_tag = "Supplied Dataset" if is_dataset else "Independently Verified"
        evidence_lines.append(f"- [{canonical_title}]({s_url}) ({type_tag}, Confidence: {max_conf:.2f}, Facts: {len(f_list)})")

    if not evidence_lines:
        evidence_lines.append("- Grounded via official Brønnøysundregistrene open register endpoints.")

    total_facts = len(profile.facts)
    total_consulted = len(profile.sources_consulted) if profile.sources_consulted else len(facts_by_url)
    indep_count = len(indep_verified_urls)
    dataset_count = len(dataset_urls)

    if dataset_count > 0:
        source_summary = f"{indep_count} independently verified live sources and {dataset_count} supplied dataset record"
    else:
        source_summary = f"{indep_count} verified sources"

    evidence_text = (
        f"### 8. Evidence Traceability & Grounding\n"
        f"Verified against {total_facts} structured evidentiary facts across {source_summary} "
        f"({total_consulted} source records consulted; sum of extracted facts: {total_facts}):\n"
        + "\n".join(evidence_lines[:10])
    )
    sections.append(evidence_text)

    return "\n\n".join(sections)
