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

    def collect(self, raw_website_url: str | None) -> OnlinePresence:
        """Fetch and extract safe verified information from the official company website."""
        norm_url = self._normalize_url(raw_website_url)
        if not norm_url:
            return OnlinePresence()

        if not self.is_allowed_by_robots(norm_url):
            logger.info("Scraping %s disallowed by robots.txt", norm_url)
            return OnlinePresence(website_url=norm_url, http_status=403)

        try:
            resp = self.client.get(
                norm_url,
                headers={"Accept": "text/html,application/xhtml+xml"},
                use_cache=True,
            )
            if resp.status_code != 200:
                return OnlinePresence(website_url=norm_url, http_status=resp.status_code)

            soup = BeautifulSoup(resp.text, "html.parser")

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

            return OnlinePresence(
                website_url=norm_url,
                http_status=resp.status_code,
                meta_description=meta_desc,
                business_summary=business_summary,
                contact_email=sorted(emails)[0] if emails else None,
                contact_phone=phone,
            )

        except Exception as e:
            logger.debug("Could not collect from website %s: %s", norm_url, e)
            return OnlinePresence(website_url=norm_url, http_status=None)
