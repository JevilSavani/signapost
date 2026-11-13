"""Official Job Postings Collector.

Discovers, verifies, and extracts:
1. Schema.org JobPosting structured JSON-LD data.
2. HTML career board listings from verified company websites.
3. Explicit verification of zero vacancies ("ingen ledige stillinger").
"""

from datetime import datetime, timezone, date
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.utils.sanitization import sanitize_untrusted_web_content
from src.models.company import JobPosting, JobStatus
from src.utils.http_client import http_client
from src.validation.identity import parse_and_verify_orgnr
from src.validation.ownership import (
    EntityOwnershipClassification,
    NORWEGIAN_MONTHS,
    ENGLISH_MONTHS,
)

logger = logging.getLogger(__name__)

ZERO_VACANCY_PATTERNS = [
    r"ingen\s+ledige\s+stillinger",
    r"for\s+tiden\s+ingen\s+ledige",
    r"har\s+for\s+tiden\s+ingen\s+ledige",
    r"ingen\s+åpne\s+stillinger",
    r"vi\s+har\s+ingen\s+ledige",
    r"no\s+open\s+positions",
    r"no\s+current\s+vacancies",
    r"currently\s+no\s+open",
    r"currently\s+no\s+vacancies",
]

ROLE_KEYWORDS = [
    "leder", "ingeniør", "utvikler", "konsulent", "stilling", "rådgiver",
    "manager", "developer", "engineer", "sjåfør", "mekaniker", "tømrer",
    "fagarbeider", "prosjektleder", "operatør", "selger", "controller",
    "regnskapsfører", "analytiker", "arkitekt", "montør", "tekniker",
]


class JobPostingCollector:
    """Collects verified active job vacancies directly from corporate web properties."""

    def __init__(self, client=http_client) -> None:
        self.client = client

    def _parse_date_string(self, raw_str: str | None) -> str | None:
        """Parse ISO, Norwegian, or English dates to YYYY-MM-DD."""
        if not raw_str:
            return None
        cleaned = raw_str.strip()

        # ISO format: YYYY-MM-DD
        m_iso = re.search(r"\b(20[2-3]\d)-([01]\d)-([0-3]\d)\b", cleaned)
        if m_iso:
            return f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}"

        # DD.MM.YYYY
        m_no = re.search(r"\b([0-3]?\d)\.([01]?\d)\.(20[2-3]\d)\b", cleaned)
        if m_no:
            d, m, y = int(m_no.group(1)), int(m_no.group(2)), int(m_no.group(3))
            return f"{y:04d}-{m:02d}-{d:02d}"

        # Textual Norwegian date: e.g. "15. mars 2026"
        for m_name, m_num in NORWEGIAN_MONTHS.items():
            pattern = rf"\b([0-3]?\d)\.?\s+{m_name}\s+(20[2-3]\d)\b"
            m = re.search(pattern, cleaned, re.I)
            if m:
                d = int(m.group(1))
                y = int(m.group(2))
                return f"{y:04d}-{m_num}-{d:02d}"

        # Textual English date: e.g. "March 15, 2026"
        for m_name, m_num in ENGLISH_MONTHS.items():
            pattern = rf"\b{m_name}\s+([0-3]?\d),?\s+(20[2-3]\d)\b"
            m = re.search(pattern, cleaned, re.I)
            if m:
                d = int(m.group(1))
                y = int(m.group(2))
                return f"{y:04d}-{m_num}-{d:02d}"

        return None

    def _determine_status(self, deadline_str: str | None, context_text: str = "") -> JobStatus:
        """Determine whether a posting is open, closed, or expired."""
        low = context_text.lower()
        if any(w in low for w in ["avsluttet", "utgått", "søknadsfrist utløpt", "closed", "expired"]):
            return JobStatus.CLOSED

        if deadline_str:
            try:
                deadline_d = date.fromisoformat(deadline_str)
                today = date.today()
                if deadline_d < today:
                    return JobStatus.EXPIRED
                return JobStatus.OPEN
            except Exception:
                pass

        return JobStatus.OPEN

    def _extract_from_json_ld(
        self,
        soup: BeautifulSoup,
        page_url: str,
        company_name: str,
        orgnr: str,
    ) -> list[JobPosting]:
        """Extract job postings from Schema.org JobPosting JSON-LD blocks."""
        postings: list[JobPosting] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
            try:
                text = script.string
                if not text:
                    continue
                data = json.loads(text.strip())
                items = data if isinstance(data, list) else [data]

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    # Check @type JobPosting or graph containing JobPosting
                    graph = item.get("@graph", [item])
                    for entity in graph:
                        if not isinstance(entity, dict):
                            continue
                        e_type = entity.get("@type", "")
                        if e_type != "JobPosting" and not (isinstance(e_type, list) and "JobPosting" in e_type):
                            continue

                        title = entity.get("title") or entity.get("name")
                        if not title:
                            continue

                        clean_title = sanitize_untrusted_web_content(str(title).strip())
                        post_date = self._parse_date_string(entity.get("datePosted"))
                        deadline = self._parse_date_string(entity.get("validThrough"))
                        job_url = entity.get("url") or page_url
                        canonical_url = urljoin(page_url, str(job_url))

                        # Extract location
                        loc_str = None
                        loc_obj = entity.get("jobLocation")
                        if isinstance(loc_obj, dict):
                            addr = loc_obj.get("address")
                            if isinstance(addr, dict):
                                loc_parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                                loc_str = ", ".join(p for p in loc_parts if p)
                            elif isinstance(addr, str):
                                loc_str = addr
                        elif isinstance(loc_obj, str):
                            loc_str = loc_obj

                        status = self._determine_status(deadline)

                        postings.append(
                            JobPosting(
                                title=clean_title,
                                canonical_url=canonical_url,
                                location=loc_str,
                                posting_date=post_date,
                                application_deadline=deadline,
                                status=status,
                                evidence=f"Schema.org JobPosting på {page_url}: {clean_title}",
                                ownership_verification=EntityOwnershipClassification.VERIFIED_EXACT_ENTITY.value,
                                retrieval_timestamp=now_iso,
                            )
                        )
            except Exception:
                continue

        return postings

    def collect_jobs(
        self,
        orgnr: str,
        company_name: str,
        website_url: str,
        is_parent_group: bool = False,
        subpages: dict[str, str] | None = None,
        max_items: int = 6,
    ) -> tuple[list[JobPosting], bool]:
        """Collect verified job postings from company career sections.

        Returns (list_of_job_postings, has_explicit_zero_vacancies).
        """
        clean_orgnr = parse_and_verify_orgnr(orgnr)
        postings: list[JobPosting] = []
        has_zero_vacancies = False
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()
        now_iso = datetime.now(timezone.utc).isoformat()

        if not website_url or not website_url.startswith("http"):
            return [], False

        career_urls: list[str] = []
        if subpages and "careers" in subpages:
            career_urls.append(subpages["careers"])

        standard_career_paths = [
            "/karriere", "/careers", "/jobb", "/stillinger",
            "/ledige-stillinger", "/arbeid", "/work-with-us", "/vacancies"
        ]
        for p in standard_career_paths:
            u = urljoin(website_url, p)
            if u not in career_urls:
                career_urls.append(u)

        for c_url in career_urls[:3]:
            try:
                resp = self.client.get(c_url, use_cache=True)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                page_text = soup.get_text(" ", strip=True).lower()

                # Check for explicit zero vacancies declaration
                for zv_pat in ZERO_VACANCY_PATTERNS:
                    if re.search(zv_pat, page_text):
                        has_zero_vacancies = True
                        break

                # 1. First attempt JSON-LD extraction
                json_ld_postings = self._extract_from_json_ld(soup, c_url, company_name, clean_orgnr)
                for p in json_ld_postings:
                    clean_u = p.canonical_url.split("?")[0].rstrip("/")
                    if clean_u not in seen_urls and p.title.lower() not in seen_titles:
                        seen_urls.add(clean_u)
                        seen_titles.add(p.title.lower())
                        postings.append(p)
                        if len(postings) >= max_items:
                            break

                if len(postings) >= max_items:
                    break

                # 2. Extract HTML job listing cards / elements
                cards = soup.find_all(
                    ["article", "div", "li", "tr"],
                    class_=re.compile(r"job|career|stilling|vacancy|position|oppdrag", re.I),
                )[:15]

                for card in cards:
                    heading = card.find(["h1", "h2", "h3", "h4", "a"])
                    link = card.find("a", href=True)
                    if not heading and not link:
                        continue

                    raw_title = heading.get_text(strip=True) if heading else link.get_text(strip=True)
                    if not raw_title or len(raw_title) < 5:
                        continue

                    # Filter out generic navigational labels
                    if any(raw_title.lower() == nav for nav in ["ledige stillinger", "karriere", "se alle stillinger", "careers"]):
                        continue

                    # Check relevance keyword or anchor link path
                    href = link["href"].strip() if link else c_url
                    full_job_url = urljoin(c_url, href)
                    clean_u = full_job_url.split("?")[0].rstrip("/")

                    is_relevant = any(kw in raw_title.lower() for kw in ROLE_KEYWORDS) or any(
                        sub in urlparse(full_job_url).path.lower()
                        for sub in ["/stilling/", "/job/", "/vacancies/", "/karriere/"]
                    )
                    if not is_relevant:
                        continue

                    if clean_u in seen_urls or raw_title.lower() in seen_titles:
                        continue

                    # Look for deadline and location in card
                    card_text = card.get_text(" ", strip=True)
                    deadline = None
                    m_deadline = re.search(r"(?:frist|søknadsfrist|deadline)[:\s]+([^\n,]+)", card_text, re.I)
                    if m_deadline:
                        deadline = self._parse_date_string(m_deadline.group(1))

                    loc = None
                    loc_tag = card.find(["span", "p", "div"], class_=re.compile(r"location|sted|arbeidssted", re.I))
                    if loc_tag:
                        loc = sanitize_untrusted_web_content(loc_tag.get_text(strip=True))

                    status = self._determine_status(deadline, card_text)
                    clean_t = sanitize_untrusted_web_content(raw_title)

                    seen_urls.add(clean_u)
                    seen_titles.add(clean_t.lower())

                    postings.append(
                        JobPosting(
                            title=clean_t,
                            canonical_url=full_job_url,
                            location=loc,
                            posting_date=None,
                            application_deadline=deadline,
                            status=status,
                            evidence=f"Stillingsannonse fra {c_url}: {clean_t}",
                            ownership_verification=(
                                EntityOwnershipClassification.VERIFIED_EXACT_ENTITY.value
                                if not is_parent_group
                                else EntityOwnershipClassification.VERIFIED_OFFICIAL_PARENT.value
                            ),
                            retrieval_timestamp=now_iso,
                        )
                    )
                    if len(postings) >= max_items:
                        break

                if len(postings) >= max_items:
                    break

            except Exception as e:
                logger.debug("Failed checking careers page %s: %s", c_url, e)

        # If we have postings, we definitely don't have zero vacancies
        if postings:
            has_zero_vacancies = False

        return postings[:max_items], has_zero_vacancies
