"""Permitted website collector with prompt-injection defense and robots compliance."""

import logging
import re
import urllib.robotparser
from typing import Any
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from src.models.company import OnlinePresence
from src.collectors.jobs import JobPostingCollector
from src.utils.http_client import http_client
from src.utils.sanitization import SUSPICIOUS_PATTERNS, sanitize_untrusted_web_content
from src.validation.ownership import (
    EVENT_OR_CALENDAR_URL_PATTERNS,
    EVENT_TITLE_PATTERNS,
    NewsValidationStatus,
    WebsiteOwnershipStatus,
    classify_website_ownership,
    extract_and_verify_social_links,
    is_credible_business_description,
    parse_publication_date_string,
    validate_news_item,
)

logger = logging.getLogger(__name__)


class WebsiteCollector:
    """Safely extracts verified public business facts from official corporate websites."""

    def __init__(self, client=http_client) -> None:
        self.client = client
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _normalize_url(self, raw_url: str | None) -> str | None:
        if not raw_url:
            return None
        url = raw_url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        return url

    def is_allowed_by_robots(self, target_url: str) -> bool:
        """Check if crawling target_url is permitted by the site's robots.txt."""
        try:
            parsed = urlparse(target_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            if base not in self._robots_cache:
                rp = urllib.robotparser.RobotFileParser()
                rp.set_url(f"{base}/robots.txt")
                try:
                    # Short timeout check for robots.txt
                    resp = self.client.get(f"{base}/robots.txt", use_cache=True)
                    if resp.status_code == 200:
                        rp.parse(resp.text.splitlines())
                    else:
                        rp.allow_all = True
                except Exception:
                    rp.allow_all = True
                self._robots_cache[base] = rp

            return self._robots_cache[base].can_fetch(self.client.DEFAULT_USER_AGENT, target_url)
        except Exception:
            return True

    def _verify_website_identity(
        self,
        soup: BeautifulSoup,
        page_text: str,
        expected_orgnr: str | None,
        expected_name: str | None,
        candidate_url: str | None = None,
        is_part_of_group: bool | None = None,
    ) -> tuple[bool, str, Any]:
        """Verify whether the webpage evidence supports ownership by the requested company."""
        res = classify_website_ownership(
            candidate_url=candidate_url,
            soup=soup,
            page_text=page_text,
            expected_orgnr=expected_orgnr,
            expected_name=expected_name,
            is_part_of_group=is_part_of_group,
        )
        return res.is_verified, res.reasoning, res

    def _discover_subpages(self, soup: BeautifulSoup, base_url: str) -> dict[str, str]:
        """Find relevant subpage links (about, careers, news) on the website."""
        subpages: dict[str, str] = {}
        parsed_base = urlparse(base_url)
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        patterns = {
            "careers": re.compile(r"/(karriere|careers|jobb|jobber|ledige-stillinger|stillinger|arbeid-hos-oss)(/|$|\?)", re.I),
            "news": re.compile(r"/(nyheter|news|presse|aktuelt|pressemeldinger|pressemelding|press-releases?|news-and-media|media)(/|$|\?)", re.I),
            "about": re.compile(r"/(om-oss|om-selskapet|om-virksomheten|about|about-us|hvem-er-vi)(/|$|\?)", re.I),
        }

        # Collect and normalize all candidate anchor links
        candidate_links: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full_url = None
            if href.startswith("/"):
                full_url = f"{base_origin}{href}"
            elif href.startswith("http://") or href.startswith("https://"):
                if urlparse(href).netloc == parsed_base.netloc:
                    full_url = href
            if full_url:
                candidate_links.append((href, full_url))

        # Sort candidate links so shorter/top-level paths are evaluated first
        candidate_links.sort(key=lambda x: len(x[0]))

        for key, pattern in patterns.items():
            for href, full_url in candidate_links:
                if key in subpages:
                    break
                path_low = urlparse(full_url).path.lower()
                if pattern.search(path_low):
                    if key == "news":
                        # Strictly do not choose calendar, event, conference, or webcast pages as the primary news endpoint
                        if any(re.search(ep, path_low) for ep in EVENT_OR_CALENDAR_URL_PATTERNS):
                            continue
                    subpages[key] = full_url

        return subpages

    def collect(
        self,
        raw_website_url: str | None,
        expected_orgnr: str | None = None,
        expected_name: str | None = None,
        crawl_subpages: bool = True,
        is_part_of_group: bool | None = None,
    ) -> OnlinePresence:
        """Fetch and extract safe verified information from the official company website."""
        norm_url = self._normalize_url(raw_website_url)
        if not norm_url:
            return OnlinePresence()

        if not self.is_allowed_by_robots(norm_url):
            logger.info("Scraping %s disallowed by robots.txt", norm_url)
            return OnlinePresence(
                website_url=norm_url,
                http_status=403,
                is_identity_verified=False,
                ownership_status="unverified",
                ownership_reasoning="Disallowed by robots.txt",
            )

        try:
            resp = self.client.get(
                norm_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                use_cache=True,
            )
            if resp.status_code != 200:
                return OnlinePresence(
                    website_url=norm_url,
                    http_status=resp.status_code,
                    is_identity_verified=False,
                    ownership_status="unverified",
                    ownership_reasoning=f"HTTP {resp.status_code} returned by host",
                )

            soup = BeautifulSoup(resp.text, "html.parser")
            page_text = soup.get_text()

            # Verify ownership & identity
            is_verified, reason, verif_res = self._verify_website_identity(
                soup,
                page_text,
                expected_orgnr,
                expected_name,
                candidate_url=norm_url,
                is_part_of_group=is_part_of_group,
            )
            logger.debug(
                "Website identity check for %s: verified=%s (%s), status=%s",
                norm_url,
                is_verified,
                reason,
                verif_res.status.value,
            )

            # Discover and verify social media links
            social_links = extract_and_verify_social_links(
                soup=soup,
                base_url=norm_url,
                expected_name=expected_name,
                expected_orgnr=expected_orgnr,
                is_parent_group_site=verif_res.is_parent_group,
                is_website_verified=is_verified,
            )

            # Remove executable / hidden elements
            for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
                tag.decompose()

            # Meta description
            meta_desc = None
            meta_tag = soup.find("meta", attrs={"name": re.compile(r"description", re.I)}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta_tag and meta_tag.get("content"):
                meta_desc = sanitize_untrusted_web_content(meta_tag["content"])

            # Business description extraction:
            # Prefer meaningful meta_description; otherwise look for substantive about/profile text.
            # Exclude navigation headings, buttons, cookie banners, and generic phrases.
            business_summary = None
            if meta_desc and is_credible_business_description(meta_desc):
                business_summary = meta_desc
            else:
                # Search for substantive About / Company Profile paragraphs
                # Remove navigation, headers, footers, forms, buttons first
                content_soup = BeautifulSoup(str(soup), "html.parser")
                for unwanted in content_soup(["nav", "header", "footer", "aside", "form", "button", "menu"]):
                    unwanted.decompose()
                for unwanted in content_soup.find_all(class_=re.compile(r"nav|menu|cookie|consent|footer|header|modal|banner", re.I)):
                    unwanted.decompose()

                for p in content_soup.find_all(["p", "section"]):
                    p_text = sanitize_untrusted_web_content(p.get_text(separator=" ", strip=True))
                    if is_credible_business_description(p_text):
                        business_summary = p_text
                        break

            # Contact email extraction
            emails: set[str] = set()
            for mailto in soup.select('a[href^="mailto:"]'):
                href = mailto.get("href", "")
                email = href.replace("mailto:", "").split("?")[0].strip()
                if email and "@" in email and "." in email:
                    emails.add(email)

            if not emails:
                found = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", resp.text[:10000])
                for e in found:
                    if not any(ext in e.lower() for ext in [".png", ".jpg", ".jpeg", ".webp", ".js"]):
                        emails.add(e)

            # Contact phone
            phone = None
            tel_tag = soup.select_one('a[href^="tel:"]')
            if tel_tag:
                phone = tel_tag.get("href", "").replace("tel:", "").strip()

            active_jobs: list[dict[str, Any]] = []
            announcements: list[dict[str, Any]] = []
            news_items: list[dict[str, Any]] = []
            has_explicit_zero_vacancies = False

            # Discover and crawl bounded subpages if enabled
            if crawl_subpages and is_verified:
                subpages = self._discover_subpages(soup, norm_url)

                # Careers / Jobs subpage via JobPostingCollector
                job_collector = JobPostingCollector(client=self.client)
                extracted_jobs, zero_vac = job_collector.collect_jobs(
                    orgnr=expected_orgnr or "",
                    company_name=expected_name or "",
                    website_url=norm_url,
                    is_parent_group=verif_res.is_parent_group,
                    subpages=subpages,
                    max_items=6,
                )
                has_explicit_zero_vacancies = zero_vac
                for job in extracted_jobs:
                    active_jobs.append({
                        "title": job.title,
                        "url": job.canonical_url,
                        "canonical_url": job.canonical_url,
                        "location": job.location,
                        "posting_date": job.posting_date,
                        "deadline": job.application_deadline,
                        "application_deadline": job.application_deadline,
                        "status": job.status.value,
                        "evidence": job.evidence,
                        "is_verified": True,
                    })

                # News / Announcements subpage
                crawled_news_url = None
                if "news" in subpages:
                    news_url = subpages["news"]
                    crawled_news_url = news_url
                    try:
                        n_resp = self.client.get(news_url, use_cache=True)
                        if n_resp.status_code == 200:
                            n_soup = BeautifulSoup(n_resp.text, "html.parser")

                            # Extract candidate article entries
                            # 1. Structural articles (<article>, <section>, card containers)
                            candidate_entries: list[tuple[str, str, str | None, bool, str | None]] = []
                            seen_art_urls: set[str] = set()

                            # First search dedicated article elements
                            article_elements = n_soup.find_all(
                                ["article", "div", "li", "section"],
                                class_=re.compile(r"news|article|post|press|entry|card", re.I),
                            )[:12]

                            for el in article_elements:
                                heading = el.find(["h1", "h2", "h3", "h4", "a"])
                                link = el.find("a", href=True)
                                time_el = el.find("time")
                                date_el = el.find(
                                    ["span", "p", "div"],
                                    class_=re.compile(r"date|time|published|pubdate", re.I),
                                )
                                raw_date = None
                                has_explicit = False
                                if time_el:
                                    raw_date = time_el.get("datetime") or time_el.get_text(strip=True)
                                    has_explicit = True
                                elif date_el:
                                    raw_date = date_el.get("datetime") or date_el.get_text(strip=True)
                                    has_explicit = True

                                event_el = el.find(
                                    ["span", "p", "div"],
                                    class_=re.compile(r"event|calendar|schedule|webcast|conference", re.I),
                                )
                                raw_ev_date = event_el.get_text(strip=True) if event_el else None

                                title_text = heading.get_text(strip=True) if heading else ""
                                art_href = link["href"].strip() if link else news_url
                                full_art_url = urljoin(news_url, art_href)
                                clean_u = full_art_url.split("?")[0].rstrip("/")

                                if title_text and len(title_text) >= 8 and clean_u not in seen_art_urls:
                                    seen_art_urls.add(clean_u)
                                    candidate_entries.append((title_text, full_art_url, raw_date, has_explicit, raw_ev_date))

                            # 2. Search anchor links matching news path slugs
                            for a in n_soup.find_all("a", href=True):
                                href = a["href"].strip()
                                full_art_url = urljoin(news_url, href)
                                clean_u = full_art_url.split("?")[0].rstrip("/")
                                if clean_u in seen_art_urls or clean_u == news_url.rstrip("/"):
                                    continue
                                path_low = urlparse(full_art_url).path.lower()
                                if any(k in path_low for k in ["/news/20", "/nyheter/20", "/pressemeldinger/20", "/press-releases/20"]):
                                    h = a.find(["h1", "h2", "h3", "h4"])
                                    t_text = h.get_text(strip=True) if h else a.get_text(strip=True)
                                    if len(t_text) >= 8:
                                        time_tag = a.find("time") or a.find_parent(["div", "section"]).find("time") if a.find_parent(["div", "section"]) else None
                                        raw_d = None
                                        has_exp = False
                                        if time_tag:
                                            raw_d = time_tag.get("datetime") or time_tag.get_text(strip=True)
                                            has_exp = True
                                        seen_art_urls.add(clean_u)
                                        candidate_entries.append((t_text, full_art_url, raw_d, has_exp, None))

                            # 3. Fallback: headings with links
                            if not candidate_entries:
                                for h in n_soup.find_all(["h2", "h3"])[:8]:
                                    parent_link = h.find("a", href=True) or h.find_parent("a", href=True)
                                    h_text = h.get_text(strip=True)
                                    art_url = urljoin(news_url, parent_link["href"]) if parent_link else news_url
                                    clean_u = art_url.split("?")[0].rstrip("/")
                                    if clean_u in seen_art_urls:
                                        continue
                                    seen_art_urls.add(clean_u)
                                    candidate_entries.append((h_text, art_url, None, False, None))

                            # Validate each candidate article
                            for t_text, a_url, r_date, has_exp, r_ev_date in candidate_entries:
                                val_res = validate_news_item(
                                    title=t_text,
                                    url=a_url,
                                    raw_date=r_date,
                                    expected_name=expected_name,
                                    expected_orgnr=expected_orgnr,
                                    source_url=news_url,
                                    is_parent_group_site=verif_res.is_parent_group,
                                    raw_event_date=r_ev_date,
                                    has_explicit_publication_evidence=has_exp,
                                )

                                news_items.append({
                                    "title": val_res.title,
                                    "url": val_res.url,
                                    "canonical_url": val_res.canonical_url,
                                    "publication_date": val_res.publication_date,
                                    "event_date": val_res.event_date,
                                    "is_event": val_res.is_event,
                                    "company_relevance_evidence": val_res.company_relevance_evidence,
                                    "source_url": val_res.source_url,
                                    "status": val_res.status.value,
                                    "rejection_reason": val_res.rejection_reason,
                                    "is_verified": val_res.status == NewsValidationStatus.VERIFIED,
                                })

                                if val_res.status == NewsValidationStatus.VERIFIED:
                                    clean_h = sanitize_untrusted_web_content(val_res.title)
                                    announcements.append({
                                        "headline": clean_h,
                                        "title": clean_h,
                                        "url": val_res.canonical_url or val_res.url,
                                        "publication_date": val_res.publication_date,
                                    })
                                    if len(announcements) >= 5:
                                        break
                    except Exception:
                        pass

            return OnlinePresence(
                website_url=norm_url,
                http_status=resp.status_code,
                meta_description=meta_desc,
                business_summary=business_summary,
                contact_email=sorted(emails)[0] if emails else None,
                contact_phone=phone,
                active_jobs=active_jobs,
                announcements=announcements,
                news_items=news_items,
                social_links=social_links,
                ownership_status=verif_res.status.value,
                ownership_reasoning=verif_res.reasoning,
                declared_orgnr=verif_res.declared_orgnr,
                is_identity_verified=is_verified,
                has_explicit_zero_vacancies=has_explicit_zero_vacancies,
                crawled_news_url=crawled_news_url,
                crawled_subpages=subpages if (crawl_subpages and is_verified) else {},
            )

        except Exception as e:
            logger.debug("Could not collect from website %s: %s", norm_url, e)
            return OnlinePresence(website_url=norm_url, http_status=None, is_identity_verified=False)
