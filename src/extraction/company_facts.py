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


def extract_profile_from_sources(
    expected_orgnr: str,
    enhet_data: dict[str, Any],
    roller_data: dict[str, Any] | None = None,
    regnskap_list: list[dict[str, Any]] | None = None,
    online_data: OnlinePresence | None = None,
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
            retrieval_timestamp=now_utc,
            publication_date=enhet_data.get("registreringsdatoEnhetsregisteret"),
            confidence=1.0,
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
        retrieval_timestamp=now_utc,
        publication_date=enhet_data.get("registreringsdatoEnhetsregisteret"),
        confidence=1.0,
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
        retrieval_timestamp=now_utc,
        confidence=1.0,
        evidence_snippet=f"konkurs={is_bankrupt}, underAvvikling={is_liquidating}, tvang={is_forced}",
    )

    reg_date = enhet_data.get("registreringsdatoEnhetsregisteret")
    if reg_date:
        facts["registration_date"] = ProvenanceFact(
            fact_key="registration_date",
            category=FactCategory.REGISTRATION,
            value=reg_date,
            source_url=enhet_url,
            retrieval_timestamp=now_utc,
            publication_date=reg_date,
            confidence=1.0,
            evidence_snippet=f"registreringsdatoEnhetsregisteret: {reg_date}",
        )

    founding_date = enhet_data.get("stiftelsesdato")
    if founding_date:
        facts["founding_date"] = ProvenanceFact(
            fact_key="founding_date",
            category=FactCategory.REGISTRATION,
            value=founding_date,
            source_url=enhet_url,
            retrieval_timestamp=now_utc,
            publication_date=founding_date,
            confidence=1.0,
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
            retrieval_timestamp=now_utc,
            confidence=1.0,
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
            retrieval_timestamp=now_utc,
            confidence=1.0,
            evidence_snippet=json.dumps(enhet_data.get("forretningsadresse")),
        )

    post_addr = build_address(enhet_data.get("postadresse"))

    # Operational & Employee facts
    emp_count = enhet_data.get("antallAnsatte")
    has_emp = enhet_data.get("harRegistrertAntallAnsatte")
    if emp_count is not None:
        facts["employee_count"] = ProvenanceFact(
            fact_key="employee_count",
            category=FactCategory.OPERATIONAL,
            value=emp_count,
            source_url=enhet_url,
            retrieval_timestamp=now_utc,
            publication_date=enhet_data.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
            confidence=1.0,
            evidence_snippet=f"antallAnsatte: {emp_count}",
        )

    is_vat = enhet_data.get("registrertIMvaregisteret")
    statutory_purpose_raw = enhet_data.get("vedtektsfestetFormaal")
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
            retrieval_timestamp=now_utc,
            publication_date=enhet_data.get("vedtektsdato"),
            confidence=1.0,
            evidence_snippet=statutory_purpose[:300],
        )

    website = enhet_data.get("hjemmeside")
    if website:
        facts["official_website"] = ProvenanceFact(
            fact_key="official_website",
            category=FactCategory.ONLINE,
            value=website,
            source_url=enhet_url,
            retrieval_timestamp=now_utc,
            confidence=1.0,
            evidence_snippet=f"hjemmeside: {website}",
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
                    p_navn = person.get("navn") or {}
                    if isinstance(p_navn, dict):
                        first_name = p_navn.get("fornavn", "")
                        last_name = p_navn.get("etternavn", "")
                    else:
                        first_name = person.get("fornavn", "")
                        last_name = person.get("etternavn", "")
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
            if ceo:
                facts["ceo_name"] = ProvenanceFact(
                    fact_key="ceo_name",
                    category=FactCategory.OFFICER,
                    value=ceo,
                    source_url=roller_url,
                    retrieval_timestamp=now_utc,
                    confidence=1.0,
                    evidence_snippet=f"Daglig leder: {ceo}",
                )
            if chair:
                facts["board_chair"] = ProvenanceFact(
                    fact_key="board_chair",
                    category=FactCategory.OFFICER,
                    value=chair,
                    source_url=roller_url,
                    retrieval_timestamp=now_utc,
                    confidence=1.0,
                    evidence_snippet=f"Styrets leder: {chair}",
                )

    # 3. Financial Statements from Regnskap
    financials: CompanyFinancials | None = None
    if regnskap_list and len(regnskap_list) > 0:
        latest = regnskap_list[0]
        # Verify identity inside regnskap object
        reg_orgnr = latest.get("virksomhet", {}).get("organisasjonsnummer", clean_orgnr)
        if str(reg_orgnr) == clean_orgnr:
            regnskap_url = f"https://data.brreg.no/regnskapsregisteret/regnskap/{clean_orgnr}"
            period = latest.get("regnskapsperiode", {})
            start_date = period.get("fraDato")
            end_date = period.get("tilDato")
            currency = latest.get("valuta", "NOK")
            statement_type = latest.get("regnskapstype", "SELSKAP")
            journal_nr = latest.get("journalnr")
            is_audited = not latest.get("revisjon", {}).get("ikkeRevidertAarsregnskap", False)

            # Revenue & Profits
            resultat = latest.get("resultatregnskapResultat", {})
            driftsresultat_dict = resultat.get("driftsresultat", {})
            driftsinntekter_dict = driftsresultat_dict.get("driftsinntekter", {})
            revenue = driftsinntekter_dict.get("sumDriftsinntekter") or driftsinntekter_dict.get("salgsinntekter")
            operating_profit = driftsresultat_dict.get("driftsresultat")
            net_income = resultat.get("aarsresultat")

            reporting_year = None
            if end_date:
                try:
                    reporting_year = int(end_date[:4])
                except Exception:
                    pass

            financials = CompanyFinancials(
                reporting_year=reporting_year,
                period_start=start_date,
                period_end=end_date,
                currency=currency,
                total_revenue=float(revenue) if revenue is not None else None,
                operating_profit=float(operating_profit) if operating_profit is not None else None,
                net_income=float(net_income) if net_income is not None else None,
                is_audited=is_audited,
                statement_type=statement_type,
                journal_number=journal_nr,
            )

            if revenue is not None:
                facts["total_revenue"] = ProvenanceFact(
                    fact_key="total_revenue",
                    category=FactCategory.FINANCIAL,
                    value=float(revenue),
                    source_url=regnskap_url,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    evidence_snippet=f"sumDriftsinntekter: {revenue} {currency} (Period: {start_date} to {end_date})",
                )

            if operating_profit is not None:
                facts["operating_profit"] = ProvenanceFact(
                    fact_key="operating_profit",
                    category=FactCategory.FINANCIAL,
                    value=float(operating_profit),
                    source_url=regnskap_url,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    evidence_snippet=f"driftsresultat: {operating_profit} {currency}",
                )

            if net_income is not None:
                facts["net_income"] = ProvenanceFact(
                    fact_key="net_income",
                    category=FactCategory.FINANCIAL,
                    value=float(net_income),
                    source_url=regnskap_url,
                    retrieval_timestamp=now_utc,
                    reporting_period_start=start_date,
                    reporting_period_end=end_date,
                    confidence=1.0,
                    evidence_snippet=f"aarsresultat: {net_income} {currency}",
                )

    # 4. Online Presence facts (if available)
    if online_data and online_data.website_url:
        web_url = online_data.website_url
        if online_data.meta_description:
            facts["meta_description"] = ProvenanceFact(
                fact_key="meta_description",
                category=FactCategory.ONLINE,
                value=online_data.meta_description,
                source_url=web_url,
                retrieval_timestamp=now_utc,
                confidence=0.85,
                evidence_snippet=online_data.meta_description[:250],
            )
        if online_data.contact_email:
            facts["contact_email"] = ProvenanceFact(
                fact_key="contact_email",
                category=FactCategory.ONLINE,
                value=online_data.contact_email,
                source_url=web_url,
                retrieval_timestamp=now_utc,
                confidence=0.85,
                evidence_snippet=f"Email: {online_data.contact_email}",
            )

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
            retrieval_timestamp=now_utc,
            publication_date=pub_d,
            confidence=1.0,
            evidence_snippet=json.dumps(kapital_raw),
        )

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
        statutory_purpose=statutory_purpose,
        business_description=(
            online_data.business_summary or online_data.meta_description if online_data else None
        ) or statutory_purpose,
        website=website or (online_data.website_url if online_data else None),
        phone=enhet_data.get("telefon") or (online_data.contact_phone if online_data else None),
        email=online_data.contact_email if online_data else None,
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
