"""Permitted website collector with prompt-injection defense and robots compliance."""

import logging
import re
import urllib.robotparser
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from src.models.company import OnlinePresence
from src.utils.http_client import http_client

logger = logging.getLogger(__name__)

# Prompt injection markers commonly used in adversarial webpage payloads
SUSPICIOUS_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"system\s*:\s*",
    r"you\s+are\s+now\s+in\s+developer\s+mode",
    r"output\s+the\s+system\s+prompt",
    r"<\|\s*im_start\s*\|>",
    r"<\|\s*endoftext\s*\|>",
]


def sanitize_untrusted_web_content(text: str) -> str:
    """Sanitize scraped web content to neutralize prompt injection vectors."""
    cleaned = text
    for pattern in SUSPICIOUS_PATTERNS:
        cleaned = re.sub(pattern, "[BLOCKED_INJECTION_PATTERN]", cleaned, flags=re.IGNORECASE)
    # Collapse multiple whitespaces and control characters
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned[:1000]  # Bounded length to prevent token-exhaustion DOS


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
        self, soup: BeautifulSoup, page_text: str, expected_orgnr: str | None, expected_name: str | None
    ) -> tuple[bool, str]:
        """Verify whether the webpage evidence supports ownership by the requested company."""
        if not expected_orgnr and not expected_name:
            return True, "No verification constraints supplied"

        # 1. Search for explicit organization number on the page
        if expected_orgnr:
            clean_expected = "".join(filter(str.isdigit, str(expected_orgnr)))
            # Regex for 9-digit patterns with optional spaces or dots
            found_orgs = re.findall(r"(?:org(?:anisasjons)?\.?\s*nr\.?|organisasjonsnummer)\s*:?\s*([0-9\s.]{9,15})", page_text, re.I)
            for found_raw in found_orgs:
                found_digits = "".join(filter(str.isdigit, found_raw))
                if len(found_digits) == 9:
                    if found_digits == clean_expected:
                        return True, f"Verified exact organization number {clean_expected} on page"
                    else:
                        return False, f"Mismatch: Page declares different organization number {found_digits}"

            # Check general presence of the 9-digit sequence
            if clean_expected in page_text.replace(" ", ""):
                return True, f"Verified organization number {clean_expected} present in page content"

        # 2. Check company name match in title, footer, or metadata
        if expected_name:
            norm_name = re.sub(r"\b(AS|ASA|ENK|BA|DA|ANS|NUF)\b", "", expected_name, flags=re.I).strip().lower()
            if len(norm_name) >= 3:
                title = (soup.title.string or "").lower() if soup.title else ""
                meta_og = soup.find("meta", attrs={"property": "og:site_name"})
                site_name = (meta_og.get("content", "")).lower() if meta_og else ""

                if norm_name in title or norm_name in site_name or norm_name in page_text.lower()[:3000]:
                    return True, f"Verified company name match '{norm_name}' in page title or metadata"

        # If domain was directly provided by official registry, allow with moderate confidence
        return False, "Could not conclusively verify company identity on website content"

    def _discover_subpages(self, soup: BeautifulSoup, base_url: str) -> dict[str, str]:
        """Find relevant subpage links (about, careers, news) on the website."""
        subpages: dict[str, str] = {}
        parsed_base = urlparse(base_url)
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

        patterns = {
            "careers": re.compile(r"/(karriere|careers|jobb|jobber|ledige-stillinger|stillinger)", re.I),
            "news": re.compile(r"/(nyheter|news|presse|aktuelt|pressemeldinger)", re.I),
            "about": re.compile(r"/(om-oss|om-selskapet|about|about-us)", re.I),
        }

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            for key, pattern in patterns.items():
                if key not in subpages and pattern.search(href):
                    if href.startswith("/"):
                        subpages[key] = f"{base_origin}{href}"
                    elif href.startswith("http://") or href.startswith("https://"):
                        if urlparse(href).netloc == parsed_base.netloc:
                            subpages[key] = href

        return subpages

    def collect(
        self,
        raw_website_url: str | None,
        expected_orgnr: str | None = None,
        expected_name: str | None = None,
        crawl_subpages: bool = True,
    ) -> OnlinePresence:
        """Fetch and extract safe verified information from the official company website."""
        norm_url = self._normalize_url(raw_website_url)
        if not norm_url:
            return OnlinePresence()

        if not self.is_allowed_by_robots(norm_url):
            logger.info("Scraping %s disallowed by robots.txt", norm_url)
            return OnlinePresence(website_url=norm_url, http_status=403, is_identity_verified=False)

        try:
            resp = self.client.get(
                norm_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                use_cache=True,
            )
            if resp.status_code != 200:
                return OnlinePresence(website_url=norm_url, http_status=resp.status_code, is_identity_verified=False)

            soup = BeautifulSoup(resp.text, "html.parser")
            page_text = soup.get_text()

            # Verify identity
            is_verified, reason = self._verify_website_identity(soup, page_text, expected_orgnr, expected_name)
            logger.debug("Website identity check for %s: verified=%s (%s)", norm_url, is_verified, reason)

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

            # Business summary from heading or paragraph text
            headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2"])][:3]
            summary_candidate = " ".join(headings)
            business_summary = sanitize_untrusted_web_content(summary_candidate) if summary_candidate else meta_desc

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

            # Discover and crawl bounded subpages if enabled
            if crawl_subpages and is_verified:
                subpages = self._discover_subpages(soup, norm_url)

                # Careers / Jobs subpage
                if "careers" in subpages:
                    careers_url = subpages["careers"]
                    try:
                        c_resp = self.client.get(careers_url, use_cache=True)
                        if c_resp.status_code == 200:
                            c_soup = BeautifulSoup(c_resp.text, "html.parser")
                            for a in c_soup.find_all("a", href=True)[:10]:
                                text = a.get_text(strip=True)
                                if len(text) > 8 and any(w in text.lower() for w in ["leder", "ingeniør", "utvikler", "konsulent", "stilling", "rådgiver", "manager", "developer", "engineer"]):
                                    clean_job = sanitize_untrusted_web_content(text)
                                    active_jobs.append({"title": clean_job, "url": careers_url})
                                    if len(active_jobs) >= 5:
                                        break
                    except Exception:
                        pass

                # News / Announcements subpage
                if "news" in subpages:
                    news_url = subpages["news"]
                    try:
                        n_resp = self.client.get(news_url, use_cache=True)
                        if n_resp.status_code == 200:
                            n_soup = BeautifulSoup(n_resp.text, "html.parser")
                            for h in n_soup.find_all(["h2", "h3"])[:5]:
                                heading_text = h.get_text(strip=True)
                                if len(heading_text) > 10:
                                    clean_h = sanitize_untrusted_web_content(heading_text)
                                    announcements.append({"headline": clean_h, "url": news_url})
                                    if len(announcements) >= 3:
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
                is_identity_verified=is_verified,
            )

        except Exception as e:
            logger.debug("Could not collect from website %s: %s", norm_url, e)
            return OnlinePresence(website_url=norm_url, http_status=None, is_identity_verified=False)
