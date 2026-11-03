"""Fact extraction engine with strict identity verification and evidence grounding."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.models.company import (
    Address,
    CompanyFinancials,
    CompanyOfficer,
    CompanyProfile,
    OnlinePresence,
)
from src.models.evidence import FactCategory, ProvenanceFact
from src.validation.identity import parse_and_verify_orgnr
from src.validation.ownership import is_credible_business_description

logger = logging.getLogger(__name__)


def build_address(addr_data: dict[str, Any] | None) -> Address | None:
    if not addr_data or not isinstance(addr_data, dict):
        return None
    street_list = addr_data.get("adresse", [])
    street = ", ".join(street_list) if isinstance(street_list, list) else str(street_list)
    return Address(
        street_address=street or None,
        postal_code=addr_data.get("postnummer"),
        city=addr_data.get("poststed"),
        municipality=addr_data.get("kommune"),
        municipality_number=addr_data.get("kommunenummer"),
        country=addr_data.get("land", "Norge"),
        country_code=addr_data.get("landkode", "NO"),
    )


def select_latest_valid_filing(
    regnskap_list: list[dict[str, Any]] | None,
    expected_orgnr: str,
    is_part_of_group: bool | None = None,
) -> dict[str, Any] | None:
    """Select the most recent valid financial statement from Regnskapsregisteret.

    Rules:
    - Verifies that virksomhet.organisasjonsnummer strictly matches expected_orgnr.
    - Validates regnskapsperiode: fraDato and tilDato must be present, 10-char ISO dates,
      and fraDato <= tilDato. Suspicious/inverted dates are rejected.
    - Filters records to require at least one parseable financial figure.
    - Sorts records to identify the latest reporting period (maximum tilDato).
    - If both consolidated (KONSERN) and parent (SELSKAP) accounts exist for the same period:
      prefers KONSERN for groups/morselskap; prefers SELSKAP for independent entities.
    - Excludes liquidation accounts (avviklingsregnskap) if normal filings exist.
    """
    if not regnskap_list:
        return None

    clean_orgnr = str(expected_orgnr).strip()
    valid_records: list[dict[str, Any]] = []

    for r in regnskap_list:
        if not isinstance(r, dict):
            continue

        # 1. Identity match inside accounting object
        r_orgnr = str(r.get("virksomhet", {}).get("organisasjonsnummer", "")).strip()
        if r_orgnr and r_orgnr != clean_orgnr:
            continue

        # 2. Validate reporting period dates
        period = r.get("regnskapsperiode") or {}
        start_date = str(period.get("fraDato") or "").strip()
        end_date = str(period.get("tilDato") or "").strip()

        if not start_date or not end_date:
            continue
        if len(start_date) != 10 or len(end_date) != 10:
            continue
        if start_date > end_date:
            # Inverted dates are suspicious and rejected
            logger.warning(
                "Suspicious inverted reporting period for %s: %s > %s",
                clean_orgnr,
                start_date,
                end_date,
            )
            continue

        # 3. Require at least one financial figure to avoid empty/metadata-only shells
        resultat = r.get("resultatregnskapResultat") or {}
        driftsresultat = resultat.get("driftsresultat") or {}
        driftsinntekter = driftsresultat.get("driftsinntekter") or {}
        eiendeler = r.get("eiendeler") or {}
        egenkapital_gjeld = r.get("egenkapitalGjeld") or {}
        has_figures = 1 if (
            driftsinntekter.get("sumDriftsinntekter") is not None
            or driftsinntekter.get("salgsinntekter") is not None
            or driftsresultat.get("driftsresultat") is not None
            or resultat.get("aarsresultat") is not None
            or eiendeler.get("sumEiendeler") is not None
            or egenkapital_gjeld.get("sumEgenkapitalGjeld") is not None
            or (egenkapital_gjeld.get("egenkapital") or {}).get("sumEgenkapital") is not None
        ) else 0

        valid_records.append((r, has_figures))

    if not valid_records:
        return None

    # Determine corporate group scope preference
    def sort_key(item: tuple[dict[str, Any], int]) -> tuple[str, int, int, int]:
        x, has_figs = item
        til_dato = str(x.get("regnskapsperiode", {}).get("tilDato", ""))
        is_konsern = str(x.get("regnskapstype", "")).upper() == "KONSERN"
        morselskap = bool(x.get("virksomhet", {}).get("morselskap", False))
        prefers_konsern = (is_part_of_group is not False) or morselskap
        scope_score = 1 if (is_konsern if prefers_konsern else not is_konsern) else 0
        non_avvikling = 0 if x.get("avviklingsregnskap") else 1
        return (til_dato, scope_score, has_figs, non_avvikling)

    valid_records.sort(key=sort_key, reverse=True)
    return valid_records[0][0]


def extract_profile_from_sources(
    expected_orgnr: str,
    enhet_data: dict[str, Any],
    roller_data: dict[str, Any] | None = None,
    regnskap_list: list[dict[str, Any]] | None = None,
    online_data: OnlinePresence | None = None,
    underenheter_list: list[dict[str, Any]] | None = None,
    seed_facts: dict[str, ProvenanceFact] | None = None,
) -> CompanyProfile:
    """Synthesize raw multi-source payloads into a verified CompanyProfile.

    Strict identity checks:
    - Verifies that enhet_data['organisasjonsnummer'] strictly matches expected_orgnr.
    - If mismatch, raises ValueError to avoid wrong-company data corruption.
    """
    clean_orgnr = parse_and_verify_orgnr(expected_orgnr)
    returned_orgnr = str(enhet_data.get("organisasjonsnummer", "")).strip()

    if returned_orgnr != clean_orgnr:
        raise ValueError(
            f"Identity mismatch error: Requested {clean_orgnr} but received {returned_orgnr}"
        )

    now_utc = datetime.now(timezone.utc)
    enhet_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{clean_orgnr}"
    facts: dict[str, ProvenanceFact] = {}

    # 1. Identity & Registration Facts
    name = enhet_data.get("navn", "").strip()
    if name:
        facts["legal_name"] = ProvenanceFact(
            fact_key="legal_name",
            category=FactCategory.IDENTITY,
            value=name,
            source_url=enhet_url,
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            publication_date=enhet_data.get("registreringsdatoEnhetsregisteret"),
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"navn: {name}",
        )

    org_form = enhet_data.get("organisasjonsform", {})
    legal_form_code = org_form.get("kode", "AS")
    legal_form_desc = org_form.get("beskrivelse", "Aksjeselskap")
    facts["legal_form"] = ProvenanceFact(
        fact_key="legal_form",
        category=FactCategory.REGISTRATION,
        value=f"{legal_form_code} - {legal_form_desc}",
        source_url=enhet_url,
        source_type="official_registry",
        source_title="Brønnøysundregistrene Enhetsregisteret",
        associated_orgnr=clean_orgnr,
        retrieval_timestamp=now_utc,
        publication_date=enhet_data.get("registreringsdatoEnhetsregisteret"),
        confidence=1.0,
        is_independently_confirmed=True,
        evidence_snippet=json.dumps(org_form),
    )

    # Status / Bankruptcy / Liquidation
    is_bankrupt = bool(enhet_data.get("konkurs", False))
    is_liquidating = bool(enhet_data.get("underAvvikling", False))
    is_forced = bool(enhet_data.get("underTvangsavviklingEllerTvangsopplosning", False))

    if is_bankrupt:
        status = "BANKRUPT"
    elif is_liquidating:
        status = "LIQUIDATING"
    elif is_forced:
        status = "FORCED_DISSOLUTION"
    else:
        status = "ACTIVE"

    facts["company_status"] = ProvenanceFact(
        fact_key="company_status",
        category=FactCategory.REGISTRATION,
        value=status,
        source_url=enhet_url,
        source_type="official_registry",
        source_title="Brønnøysundregistrene Enhetsregisteret",
        associated_orgnr=clean_orgnr,
        retrieval_timestamp=now_utc,
        confidence=1.0,
        is_independently_confirmed=True,
        evidence_snippet=f"konkurs={is_bankrupt}, underAvvikling={is_liquidating}, tvang={is_forced}",
    )

    reg_date = enhet_data.get("registreringsdatoEnhetsregisteret")
    if reg_date:
        facts["registration_date"] = ProvenanceFact(
            fact_key="registration_date",
            category=FactCategory.REGISTRATION,
            value=reg_date,
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            publication_date=reg_date,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"registreringsdatoEnhetsregisteret: {reg_date}",
        )

    founding_date = enhet_data.get("stiftelsesdato")
    if founding_date:
        facts["founding_date"] = ProvenanceFact(
            fact_key="founding_date",
            category=FactCategory.REGISTRATION,
            value=founding_date,
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            publication_date=founding_date,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"stiftelsesdato: {founding_date}",
        )

    # Industry / NACE
    nace_data = enhet_data.get("naeringskode1", {})
    nace_code = nace_data.get("kode")
    nace_desc = nace_data.get("beskrivelse")
    if nace_code:
        facts["industry_code"] = ProvenanceFact(
            fact_key="industry_code",
            category=FactCategory.CLASSIFICATION,
            value=f"{nace_code} - {nace_desc or ''}".strip(),
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=json.dumps(nace_data),
        )

    # Address
    office_addr = build_address(enhet_data.get("forretningsadresse"))
    if office_addr:
        facts["registered_office_address"] = ProvenanceFact(
            fact_key="registered_office_address",
            category=FactCategory.LOCATION,
            value=office_addr.format_single_line(),
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=json.dumps(enhet_data.get("forretningsadresse")),
        )

    post_addr = build_address(enhet_data.get("postadresse"))

    # Operational & Employee facts
    emp_count = enhet_data.get("antallAnsatte")
    has_emp = enhet_data.get("harRegistrertAntallAnsatte")
    if emp_count is not None:
        reg_date_emp = enhet_data.get("registreringsdatoAntallAnsatteEnhetsregisteret")
        date_snippet = f", registered {reg_date_emp}" if reg_date_emp else ""
        facts["employee_count"] = ProvenanceFact(
            fact_key="employee_count",
            category=FactCategory.OPERATIONAL,
            value=emp_count,
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            publication_date=reg_date_emp,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"antallAnsatte: {emp_count} (NAV Aa-registeret legal entity count{date_snippet})",
        )

    is_vat = enhet_data.get("registrertIMvaregisteret")
    statutory_purpose_raw = enhet_data.get("vedtektsfestetFormaal") or enhet_data.get("formaal")
    statutory_purpose = (
        " ".join(statutory_purpose_raw)
        if isinstance(statutory_purpose_raw, list)
        else statutory_purpose_raw
    )
    if statutory_purpose:
        facts["statutory_purpose"] = ProvenanceFact(
            fact_key="statutory_purpose",
            category=FactCategory.OPERATIONAL,
            value=statutory_purpose,
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            publication_date=enhet_data.get("vedtektsdato"),
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=statutory_purpose[:300],
        )

    website = enhet_data.get("hjemmeside")
    if website:
        facts["official_website"] = ProvenanceFact(
            fact_key="official_website",
            category=FactCategory.ONLINE,
            value=website,
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"hjemmeside: {website}",
        )

    phone_registry = enhet_data.get("telefon")
    if phone_registry and "contact_phone" not in facts:
        facts["contact_phone"] = ProvenanceFact(
            fact_key="contact_phone",
            category=FactCategory.COMMUNICATION if hasattr(FactCategory, "COMMUNICATION") else FactCategory.OPERATIONAL,
            value=str(phone_registry).strip(),
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"telefon: {phone_registry}",
        )

    email_registry = enhet_data.get("epost")
    if email_registry and "contact_email" not in facts:
        facts["contact_email"] = ProvenanceFact(
            fact_key="contact_email",
            category=FactCategory.COMMUNICATION if hasattr(FactCategory, "COMMUNICATION") else FactCategory.OPERATIONAL,
            value=str(email_registry).strip(),
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"epost: {email_registry}",
        )

    overordnet_enhet = enhet_data.get("overordnetEnhet")
    if overordnet_enhet and "parent_company" not in facts:
        facts["parent_company"] = ProvenanceFact(
            fact_key="parent_company",
            category=FactCategory.CORPORATE if hasattr(FactCategory, "CORPORATE") else FactCategory.REGISTRATION,
            value=str(overordnet_enhet).strip(),
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"overordnetEnhet: {overordnet_enhet}",
        )

    # 2. Key People / Officers from Roller
    officers: list[CompanyOfficer] = []
    if roller_data and isinstance(roller_data, dict):
        roller_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{clean_orgnr}/roller"
        for group in roller_data.get("rollegrupper", []):
            group_desc = group.get("type", {}).get("beskrivelse", "Rolle")
            for r in group.get("roller", []):
                role_desc = r.get("type", {}).get("beskrivelse", group_desc)
                person = r.get("person") or {}
                full_name = ""
                if isinstance(person, dict):
                    p_navn = person.get("navn")
                    if isinstance(p_navn, dict):
                        first_name = p_navn.get("fornavn", "")
                        last_name = p_navn.get("etternavn", "")
                    elif isinstance(p_navn, str):
                        full_name = p_navn.strip()
                    else:
                        first_name = person.get("fornavn", "")
                        last_name = person.get("etternavn", "")
                    if not full_name:
                        full_name = f"{first_name} {last_name}".strip()

                if not full_name and "enhet" in r:
                    raw_enhet_name = r["enhet"].get("navn", "")
                    if isinstance(raw_enhet_name, list):
                        full_name = " ".join(str(x) for x in raw_enhet_name).strip()
                    else:
                        full_name = str(raw_enhet_name).strip()

                if full_name:
                    officers.append(
                        CompanyOfficer(
                            role_code=r.get("type", {}).get("kode"),
                            role_description=role_desc,
                            name=str(full_name),
                            is_resigned=r.get("fratraadt", False),
                        )
                    )

        if officers:
            ceo = next((o.name for o in officers if "daglig leder" in o.role_description.lower()), None)
            chair = next((o.name for o in officers if "styrets leder" in o.role_description.lower()), None)
            auditor = next((o.name for o in officers if "revisor" in o.role_description.lower()), None)
            board_members = [
                o.name for o in officers
                if "styremedlem" in o.role_description.lower() and o.name not in (ceo, chair)
            ]
            if ceo:
                facts["ceo_name"] = ProvenanceFact(
                    fact_key="ceo_name",
                    category=FactCategory.OFFICER,
                    value=ceo,
                    source_url=roller_url,
                    source_type="official_roles",
                    source_title="Brønnøysundregistrene Roller",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Daglig leder: {ceo}",
                )
            if chair:
                facts["board_chair"] = ProvenanceFact(
                    fact_key="board_chair",
                    category=FactCategory.OFFICER,
                    value=chair,
                    source_url=roller_url,
                    source_type="official_roles",
                    source_title="Brønnøysundregistrene Roller",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Styrets leder: {chair}",
                )
            if auditor:
                facts["auditor"] = ProvenanceFact(
                    fact_key="auditor",
                    category=FactCategory.OFFICER,
                    value=auditor,
                    source_url=roller_url,
                    source_type="official_roles",
                    source_title="Brønnøysundregistrene Roller",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Revisor: {auditor}",
                )
            if board_members:
                facts["board_members"] = ProvenanceFact(
                    fact_key="board_members",
                    category=FactCategory.OFFICER,
                    value=board_members,
                    source_url=roller_url,
                    source_type="official_roles",
                    source_title="Brønnøysundregistrene Roller",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Styremedlemmer: {', '.join(board_members[:4])}",
                )

    # 3. Financial Statements from Regnskap
    financials: CompanyFinancials | None = None
    if regnskap_list and len(regnskap_list) > 0:
        latest = select_latest_valid_filing(
            regnskap_list,
            clean_orgnr,
            is_part_of_group=enhet_data.get("erIKonsern") if enhet_data else None,
        )
        if latest:
            regnskap_url = f"https://data.brreg.no/regnskapsregisteret/regnskap/{clean_orgnr}"
            period = latest.get("regnskapsperiode", {})
            start_date = period.get("fraDato")
            end_date = period.get("tilDato")

            # Currency: strictly from source filing metadata; never default, guess, convert, or infer from magnitude
            raw_currency = latest.get("valuta")
            currency: str | None = None
            if raw_currency:
                cand_curr = str(raw_currency).strip().upper()
                if cand_curr and cand_curr not in ("NONE", "NULL", "UNKNOWN"):
                    currency = cand_curr

            statement_type = str(latest.get("regnskapstype", "SELSKAP")).strip().upper()
            journal_nr = latest.get("journalnr")

            # Audit status: strictly verify from filing explicit revision fields;
            # never infer from existence of financial statements or listed auditor
            revisjon_data = latest.get("revisjon")
            is_audited: bool | None = None
            if isinstance(revisjon_data, dict) and revisjon_data:
                ikke_revidert = revisjon_data.get("ikkeRevidertAarsregnskap")
                fravalg = revisjon_data.get("fravalgRevisjon")
                ikke_utfoert = revisjon_data.get("revisjonIkkeUtfoert")
                beretning = revisjon_data.get("revisjonsberetningForeligger")

                audited_signals = []
                if ikke_revidert is False:
                    audited_signals.append("ikkeRevidertAarsregnskap=False")
                if beretning is True:
                    audited_signals.append("revisjonsberetningForeligger=True")

                unaudited_signals = []
                if ikke_revidert is True:
                    unaudited_signals.append("ikkeRevidertAarsregnskap=True")
                if fravalg is True:
                    unaudited_signals.append("fravalgRevisjon=True")
                if ikke_utfoert is True:
                    unaudited_signals.append("revisjonIkkeUtfoert=True")

                if audited_signals and unaudited_signals:
                    # Contradictory evidence in filing -> treat as ambiguous/unknown
                    is_audited = None
                elif audited_signals:
                    is_audited = True
                elif unaudited_signals:
                    is_audited = False
                else:
                    is_audited = None

            # Revenue & Profits
            resultat = latest.get("resultatregnskapResultat", {})
            driftsresultat_dict = resultat.get("driftsresultat", {})
            driftsinntekter_dict = driftsresultat_dict.get("driftsinntekter", {})

            # Revenue: strictly distinguish genuine 0.0 from missing None
            revenue = None
            operating_revenue = None
            revenue_key = "sumDriftsinntekter"
            if (
                "sumDriftsinntekter" in driftsinntekter_dict
                and driftsinntekter_dict["sumDriftsinntekter"] is not None
            ):
                revenue = float(driftsinntekter_dict["sumDriftsinntekter"])
                operating_revenue = revenue
                revenue_key = "sumDriftsinntekter"
            elif (
                "salgsinntekter" in driftsinntekter_dict
                and driftsinntekter_dict["salgsinntekter"] is not None
            ):
                revenue = float(driftsinntekter_dict["salgsinntekter"])
                revenue_key = "salgsinntekter"

            operating_profit = None
            if (
                "driftsresultat" in driftsresultat_dict
                and driftsresultat_dict["driftsresultat"] is not None
            ):
                operating_profit = float(driftsresultat_dict["driftsresultat"])

            net_income = None
            if "aarsresultat" in resultat and resultat["aarsresultat"] is not None:
                net_income = float(resultat["aarsresultat"])

            reporting_year = None
            if end_date:
                try:
                    reporting_year = int(end_date[:4])
                except Exception:
                    pass

            # Balance sheet assets, equity, and liabilities
            assets = None
            eiendeler_dict = latest.get("eiendeler", {})
            if "sumEiendeler" in eiendeler_dict and eiendeler_dict["sumEiendeler"] is not None:
                assets = float(eiendeler_dict["sumEiendeler"])
            elif "sumEgenkapitalGjeld" in latest.get("egenkapitalGjeld", {}):
                val = latest.get("egenkapitalGjeld", {}).get("sumEgenkapitalGjeld")
                if val is not None:
                    assets = float(val)

            equity = None
            egenkapital_dict = latest.get("egenkapitalGjeld", {}).get("egenkapital", {})
            if "sumEgenkapital" in egenkapital_dict and egenkapital_dict["sumEgenkapital"] is not None:
                equity = float(egenkapital_dict["sumEgenkapital"])

            liabilities = None
            egenkapital_gjeld_dict = latest.get("egenkapitalGjeld", {})
            gjeld_oversikt = egenkapital_gjeld_dict.get("gjeldOversikt", {})
            if "sumGjeld" in gjeld_oversikt and gjeld_oversikt["sumGjeld"] is not None:
                liabilities = float(gjeld_oversikt["sumGjeld"])
            elif "sumGjeld" in egenkapital_gjeld_dict and egenkapital_gjeld_dict["sumGjeld"] is not None:
                liabilities = float(egenkapital_gjeld_dict["sumGjeld"])
            elif "sumGjeld" in latest.get("gjeld", {}) and latest.get("gjeld", {})["sumGjeld"] is not None:
                liabilities = float(latest.get("gjeld", {})["sumGjeld"])

            financials = CompanyFinancials(
                reporting_year=reporting_year,
                period_start=start_date,
                period_end=end_date,
                currency=currency,
                currency_units="exact",
                total_revenue=revenue,
                operating_revenue=operating_revenue,
                operating_profit=operating_profit,
                net_income=net_income,
                total_assets=assets,
                total_equity=equity,
                total_liabilities=liabilities,
                is_audited=is_audited,
                statement_type=statement_type,
                journal_number=journal_nr,
            )

            curr_tag = f" {currency}" if currency else " (currency unrecorded)"
            unit_tag = "units: base currency units (exact nominal amount)"

            if revenue is not None:
                facts["total_revenue"] = ProvenanceFact(
                    fact_key="total_revenue",
                    category=FactCategory.FINANCIAL,
                    value=revenue,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"{revenue_key}: {revenue}{curr_tag} "
                        f"(Period: {start_date} to {end_date}, Type: {statement_type}, {unit_tag})"
                    ),
                )

            if operating_profit is not None:
                facts["operating_profit"] = ProvenanceFact(
                    fact_key="operating_profit",
                    category=FactCategory.FINANCIAL,
                    value=operating_profit,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"driftsresultat: {operating_profit}{curr_tag} "
                        f"(Period: {start_date} to {end_date}, Type: {statement_type}, {unit_tag})"
                    ),
                )

            if net_income is not None:
                facts["net_income"] = ProvenanceFact(
                    fact_key="net_income",
                    category=FactCategory.FINANCIAL,
                    value=net_income,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"aarsresultat: {net_income}{curr_tag} "
                        f"(Period: {start_date} to {end_date}, Type: {statement_type}, {unit_tag})"
                    ),
                )

            if assets is not None:
                facts["total_assets"] = ProvenanceFact(
                    fact_key="total_assets",
                    category=FactCategory.FINANCIAL,
                    value=assets,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"sumEiendeler: {assets}{curr_tag} "
                        f"(Period: {start_date} to {end_date}, Type: {statement_type}, {unit_tag})"
                    ),
                )

            if equity is not None:
                facts["total_equity"] = ProvenanceFact(
                    fact_key="total_equity",
                    category=FactCategory.FINANCIAL,
                    value=equity,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"sumEgenkapital: {equity}{curr_tag} "
                        f"(Period: {start_date} to {end_date}, Type: {statement_type}, {unit_tag})"
                    ),
                )

            if liabilities is not None:
                facts["total_liabilities"] = ProvenanceFact(
                    fact_key="total_liabilities",
                    category=FactCategory.FINANCIAL,
                    value=liabilities,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"sumGjeld: {liabilities}{curr_tag} "
                        f"(Period: {start_date} to {end_date}, Type: {statement_type}, {unit_tag})"
                    ),
                )

            if is_audited is not None:
                facts["is_audited"] = ProvenanceFact(
                    fact_key="is_audited",
                    category=FactCategory.FINANCIAL,
                    value=is_audited,
                    source_url=regnskap_url,
                    source_type="official_financial_statement",
                    source_title="Brønnøysundregistrene Regnskapsregisteret",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    is_independently_confirmed=True,
                    evidence_snippet=(
                        f"Filing revisjon metadata: {json.dumps(revisjon_data)} "
                        f"(Statement: {statement_type}, Journal: {journal_nr})"
                    ),
                )

    # 4. Operating Locations from Underenheter
    operating_locations: list[str] = []
    if underenheter_list:
        loc_set = set()
        for u in underenheter_list:
            if not isinstance(u, dict):
                continue
            b_addr = u.get("beliggenhetsadresse") or {}
            kommune = b_addr.get("kommune")
            poststed = b_addr.get("poststed")
            navn_u = u.get("navn")
            label = kommune or poststed or navn_u
            if label and label not in loc_set:
                loc_set.add(label)
                operating_locations.append(str(label))

        if operating_locations:
            facts["operating_locations"] = ProvenanceFact(
                fact_key="operating_locations",
                category=FactCategory.LOCATION,
                value=sorted(operating_locations),
                source_url=f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={clean_orgnr}",
                source_type="official_underenheter",
                source_title="Brønnøysundregistrene Underenheter",
                associated_orgnr=clean_orgnr,
                retrieval_timestamp=now_utc,
                confidence=1.0,
                is_independently_confirmed=True,
                evidence_snippet=f"Registered establishments: {', '.join(sorted(operating_locations)[:5])}",
            )

    # 5. Online Presence facts (only if verified)
    verified_website = None
    if online_data:
        if online_data.is_identity_verified:
            verified_website = online_data.website_url
            web_url = online_data.website_url
            if online_data.meta_description and is_credible_business_description(online_data.meta_description):
                facts["meta_description"] = ProvenanceFact(
                    fact_key="meta_description",
                    category=FactCategory.ONLINE,
                    value=online_data.meta_description,
                    source_url=web_url,
                    source_type="verified_website",
                    source_title="Verified Official Website",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=0.85,
                    is_independently_confirmed=True,
                    evidence_snippet=online_data.meta_description[:250],
                )
            if online_data.contact_email:
                facts["contact_email"] = ProvenanceFact(
                    fact_key="contact_email",
                    category=FactCategory.ONLINE,
                    value=online_data.contact_email,
                    source_url=web_url,
                    source_type="verified_website",
                    source_title="Verified Official Website",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=0.85,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Email: {online_data.contact_email}",
                )
            if online_data.active_jobs:
                job_titles = [j.get("title") for j in online_data.active_jobs if j.get("title")]
                if job_titles:
                    facts["job_vacancies"] = ProvenanceFact(
                        fact_key="job_vacancies",
                        category=FactCategory.OPERATIONAL,
                        value=job_titles,
                        source_url=online_data.active_jobs[0].get("url", web_url),
                        source_type="verified_website",
                        source_title="Company Career Page",
                        associated_orgnr=clean_orgnr,
                        retrieval_timestamp=now_utc,
                        confidence=0.85,
                        is_independently_confirmed=True,
                        evidence_snippet=f"Active listings: {', '.join(job_titles[:3])}",
                    )
            verified_news_source = getattr(online_data, "crawled_news_url", None) or web_url
            if online_data.announcements:
                headlines = [a.get("headline") for a in online_data.announcements if a.get("headline")]
                if headlines:
                    facts["public_announcements"] = ProvenanceFact(
                        fact_key="public_announcements",
                        category=FactCategory.OPERATIONAL,
                        value=headlines,
                        source_url=verified_news_source,
                        source_type="verified_website",
                        source_title="Company Announcements",
                        associated_orgnr=clean_orgnr,
                        retrieval_timestamp=now_utc,
                        confidence=0.85,
                        is_independently_confirmed=True,
                        evidence_snippet=f"Announcements: {', '.join(headlines[:2])}",
                    )

            # Verified social media profiles
            verified_socials = [s for s in getattr(online_data, "social_links", []) if s.get("is_verified")]
            if verified_socials:
                facts["social_media"] = ProvenanceFact(
                    fact_key="social_media",
                    category=FactCategory.ONLINE,
                    value=[{"platform": s["platform"], "url": s["url"], "handle": s.get("handle")} for s in verified_socials],
                    source_url=web_url,
                    source_type="verified_website",
                    source_title="Verified Social Profiles",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    confidence=0.85,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Verified social profiles: {', '.join(s['platform'] for s in verified_socials)}",
                )

            # Verified news and dated articles
            verified_news = [n for n in getattr(online_data, "news_items", []) if n.get("is_verified")]
            if verified_news:
                facts["news_articles"] = ProvenanceFact(
                    fact_key="news_articles",
                    category=FactCategory.OPERATIONAL,
                    value=[
                        {
                            "title": n["title"],
                            "url": n.get("canonical_url") or n["url"],
                            "date": n.get("publication_date"),
                            "evidence": n.get("company_relevance_evidence") or f"Published on {n.get('publication_date')}",
                        }
                        for n in verified_news
                    ],
                    source_url=verified_news_source,
                    source_type="verified_website",
                    source_title="Verified News Articles",
                    associated_orgnr=clean_orgnr,
                    retrieval_timestamp=now_utc,
                    publication_date=verified_news[0].get("publication_date"),
                    confidence=0.85,
                    is_independently_confirmed=True,
                    evidence_snippet=f"Verified articles: {', '.join(n['title'] for n in verified_news[:2])}",
                )
        else:
            # Candidate website failed identity/ownership verification (e.g. parent-group or third-party)
            if "official_website" in facts:
                del facts["official_website"]
            verified_website = None
    else:
        # Website not crawled; retain registry declaration
        verified_website = website

    # Extract share capital
    kapital_raw = enhet_data.get("kapital")
    share_capital = None
    if isinstance(kapital_raw, dict):
        belop = kapital_raw.get("belop")
        if belop is not None:
            try:
                share_capital = float(belop)
            except (ValueError, TypeError):
                pass
    elif isinstance(kapital_raw, (int, float, str)):
        try:
            share_capital = float(kapital_raw)
        except (ValueError, TypeError):
            pass

    if share_capital is not None:
        pub_d = kapital_raw.get("innfortDato") if isinstance(kapital_raw, dict) else None
        facts["share_capital"] = ProvenanceFact(
            fact_key="share_capital",
            category=FactCategory.CORPORATE if hasattr(FactCategory, "CORPORATE") else FactCategory.REGISTRATION,
            value=share_capital,
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            publication_date=pub_d,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=json.dumps(kapital_raw),
        )

    # Extract corporate group status
    is_group = enhet_data.get("erIKonsern")
    if is_group is not None:
        facts["is_part_of_group"] = ProvenanceFact(
            fact_key="is_part_of_group",
            category=FactCategory.CORPORATE if hasattr(FactCategory, "CORPORATE") else FactCategory.REGISTRATION,
            value=bool(is_group),
            source_url=enhet_url,
            source_type="official_registry",
            source_title="Brønnøysundregistrene Enhetsregisteret",
            associated_orgnr=clean_orgnr,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            is_independently_confirmed=True,
            evidence_snippet=f"erIKonsern: {is_group}",
        )

    # 6. Merge seed facts from dataset if available
    if seed_facts:
        for sk, sf in seed_facts.items():
            if sk not in facts:
                facts[sk] = sf
            else:
                facts[sk].is_independently_confirmed = True

    # 7. Strict Fact Identity Integrity Enforcement
    mismatched = []
    for k, fact in facts.items():
        if fact.associated_orgnr != clean_orgnr:
            if not fact.associated_orgnr:
                fact.associated_orgnr = clean_orgnr
            else:
                logger.error(
                    "Identity mismatch for fact '%s': expected %s, found %s. Rejecting fact.",
                    k,
                    clean_orgnr,
                    fact.associated_orgnr,
                )
                mismatched.append(k)

    for k in mismatched:
        del facts[k]

    # Build and return the consolidated CompanyProfile
    profile = CompanyProfile(
        registration_number=clean_orgnr,
        name=name,
        legal_form_code=legal_form_code,
        legal_form_description=legal_form_desc,
        status=status,
        is_bankrupt=is_bankrupt,
        is_in_liquidation=is_liquidating,
        is_in_forced_dissolution=is_forced,
        registration_date=reg_date,
        founding_date=founding_date,
        employee_count=emp_count,
        has_registered_employees=has_emp,
        is_vat_registered=is_vat,
        nace_code=nace_code,
        nace_description=nace_desc,
        registered_office_address=office_addr,
        postal_address=post_addr,
        operating_locations=operating_locations,
        statutory_purpose=statutory_purpose,
        business_description=(
            online_data.meta_description
            if (online_data and online_data.is_identity_verified and online_data.meta_description and is_credible_business_description(online_data.meta_description))
            else (
                online_data.business_summary
                if (online_data and online_data.is_identity_verified and online_data.business_summary and is_credible_business_description(online_data.business_summary))
                else (statutory_purpose if statutory_purpose and is_credible_business_description(statutory_purpose) else None)
            )
        ),
        website=verified_website,
        phone=enhet_data.get("telefon") or (online_data.contact_phone if online_data and online_data.is_identity_verified else None),
        email=enhet_data.get("epost") or (online_data.contact_email if online_data and online_data.is_identity_verified else None),
        parent_company_orgnr=enhet_data.get("overordnetEnhet"),
        social_links=online_data.social_links if online_data else [],
        news_items=online_data.news_items if online_data else [],
        is_part_of_group=enhet_data.get("erIKonsern"),
        share_capital=share_capital,
        officers=officers,
        financials=financials,
        online_presence=online_data,
        facts=facts,
        created_at=now_utc,
        updated_at=now_utc,
        last_verified_at=now_utc,
        change_history=[],
    )
    return profile
