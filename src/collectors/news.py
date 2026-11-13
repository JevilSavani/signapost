"""Official Company News & Statutory Public Announcement Collector.

Discovers, verifies, and extracts:
1. Editorial company news from verified corporate website newsrooms (/nyheter, /news, /pressemeldinger).
2. Statutory public announcements from the official Brønnøysund Register Centre (Kunngjøringer).
"""

from datetime import datetime, timezone
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.utils.sanitization import sanitize_untrusted_web_content
from src.models.company import CompanyNewsItem
from src.utils.http_client import http_client
from src.validation.identity import parse_and_verify_orgnr
from src.validation.ownership import (
    EntityOwnershipClassification,
    NewsValidationStatus,
    validate_news_item,
)

logger = logging.getLogger(__name__)


class CompanyNewsCollector:
    """Collects verified news and public statutory announcements for Norwegian entities."""

    def __init__(self, client=http_client) -> None:
        self.client = client
        self.brreg_kunngjoring_url = "https://w2.brreg.no/kunngjoring/hent.jsp"

    def fetch_statutory_announcements(
        self,
        orgnr: str,
        company_name: str,
        max_items: int = 5,
    ) -> list[CompanyNewsItem]:
        """Fetch verified statutory public announcements from Brønnøysundregistrene."""
        clean_orgnr = parse_and_verify_orgnr(orgnr)
        items: list[CompanyNewsItem] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            resp = self.client.client.post(
                self.brreg_kunngjoring_url,
                data={"orgnr": clean_orgnr},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.debug("Kunngjoringer status %d for %s", resp.status_code, clean_orgnr)
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.find_all("tr")

            for tr in rows:
                cells = [t.get_text(" ", strip=True) for t in tr.find_all("td") if t.get_text(strip=True)]
                if len(cells) < 2:
                    continue

                raw_date = cells[0]
                # Date format is DD.MM.YYYY
                date_m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", raw_date)
                if not date_m:
                    continue

                day, month, year = date_m.groups()
                pub_date = f"{year}-{month}-{day}"

                # Announcement type text
                announcement_type = cells[1]
                if not announcement_type or len(announcement_type) < 3:
                    continue

                clean_type = sanitize_untrusted_web_content(announcement_type)
                headline = f"{company_name}: Offisiell kunngjøring – {clean_type}"
                canonical_url = f"https://w2.brreg.no/kunngjoring/hent_en.jsp?orgnr={clean_orgnr}"
                evidence = (
                    f"Brønnøysundregistrene kunngjøring for {company_name} "
                    f"({clean_orgnr}) datert {raw_date}: {clean_type}"
                )

                item = CompanyNewsItem(
                    headline=headline,
                    canonical_url=canonical_url,
                    publication_date=pub_date,
                    publisher="Brønnøysundregistrene",
                    summary=f"Offisielt registrert kunngjøring: {clean_type}.",
                    evidence=evidence,
                    item_type="statutory_announcement",
                    ownership_verification=EntityOwnershipClassification.VERIFIED_EXACT_ENTITY.value,
                    retrieval_timestamp=now_iso,
                )
                items.append(item)
                if len(items) >= max_items:
                    break

        except Exception as e:
            logger.debug("Could not fetch statutory announcements for %s: %s", clean_orgnr, e)

        return items

    def fetch_website_news(
        self,
        orgnr: str,
        company_name: str,
        website_url: str,
        is_parent_group: bool = False,
        max_items: int = 5,
    ) -> list[CompanyNewsItem]:
        """Fetch and validate news articles directly from the company's verified website."""
        clean_orgnr = parse_and_verify_orgnr(orgnr)
        items: list[CompanyNewsItem] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        if not website_url or not website_url.startswith("http"):
            return []

        news_paths = ["/nyheter", "/news", "/pressemeldinger", "/aktuelt", "/press"]
        discovered_urls: set[str] = set()

        for path in news_paths:
            target_url = urljoin(website_url, path)
            try:
                resp = self.client.get(target_url, use_cache=True)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                articles = soup.find_all(
                    ["article", "div", "li", "section"],
                    class_=re.compile(r"news|article|post|press|entry|card", re.I),
                )[:10]

                for art in articles:
                    heading = art.find(["h1", "h2", "h3", "h4", "a"])
                    link = art.find("a", href=True)
                    time_tag = art.find("time")
                    date_tag = art.find(
                        ["span", "p", "div"],
                        class_=re.compile(r"date|time|published|pubdate", re.I),
                    )

                    raw_d = None
                    has_exp = False
                    if time_tag:
                        raw_d = time_tag.get("datetime") or time_tag.get_text(strip=True)
                        has_exp = True
                    elif date_tag:
                        raw_d = date_tag.get("datetime") or date_tag.get_text(strip=True)
                        has_exp = True

                    event_tag = art.find(
                        ["span", "p", "div"],
                        class_=re.compile(r"event|calendar|schedule|webcast", re.I),
                    )
                    raw_ev = event_tag.get_text(strip=True) if event_tag else None

                    title_str = heading.get_text(strip=True) if heading else ""
                    href = link["href"].strip() if link else target_url
                    full_url = urljoin(target_url, href)
                    clean_u = full_url.split("?")[0].rstrip("/")

                    if not title_str or len(title_str) < 8 or clean_u in discovered_urls:
                        continue

                    # Validate article
                    val_res = validate_news_item(
                        title=title_str,
                        url=full_url,
                        raw_date=raw_d,
                        expected_name=company_name,
                        expected_orgnr=clean_orgnr,
                        source_url=target_url,
                        is_parent_group_site=is_parent_group,
                        raw_event_date=raw_ev,
                        has_explicit_publication_evidence=has_exp,
                    )

                    if val_res.status == NewsValidationStatus.VERIFIED:
                        discovered_urls.add(clean_u)
                        clean_head = sanitize_untrusted_web_content(val_res.title)
                        snippet = val_res.company_relevance_evidence or f"Artikkel publisert på {website_url}: {clean_head}"

                        item = CompanyNewsItem(
                            headline=clean_head,
                            canonical_url=val_res.canonical_url or full_url,
                            publication_date=val_res.publication_date,
                            publisher=urlparse(website_url).netloc,
                            summary=clean_head,
                            evidence=snippet,
                            item_type="editorial_news",
                            ownership_verification=(
                                EntityOwnershipClassification.VERIFIED_EXACT_ENTITY.value
                                if not is_parent_group
                                else EntityOwnershipClassification.VERIFIED_OFFICIAL_PARENT.value
                            ),
                            retrieval_timestamp=now_iso,
                        )
                        items.append(item)
                        if len(items) >= max_items:
                            break

                if len(items) >= max_items:
                    break

            except Exception as e:
                logger.debug("Failed crawling %s for news: %s", target_url, e)

        return items

    def collect_all_news(
        self,
        orgnr: str,
        company_name: str,
        website_url: str | None = None,
        is_parent_group: bool = False,
        online_news: list[dict[str, Any]] | None = None,
        max_total: int = 8,
    ) -> list[CompanyNewsItem]:
        """Aggregate news items from website and official registry announcements."""
        combined: list[CompanyNewsItem] = []
        seen_urls: set[str] = set()

        # 1. Official website news (already crawled or freshly collected)
        if online_news:
            for n in online_news:
                if not n.get("is_verified"):
                    continue
                url = n.get("canonical_url") or n.get("url") or ""
                clean_u = url.split("?")[0].rstrip("/")
                if clean_u and clean_u not in seen_urls:
                    seen_urls.add(clean_u)
                    combined.append(
                        CompanyNewsItem(
                            headline=n.get("title", ""),
                            canonical_url=url,
                            publication_date=n.get("publication_date"),
                            publisher=urlparse(url).netloc if url else company_name,
                            summary=n.get("title", ""),
                            evidence=n.get("company_relevance_evidence") or f"Nyhet fra selskapets nettsted: {n.get('title')}",
                            item_type="editorial_news",
                            ownership_verification=EntityOwnershipClassification.VERIFIED_EXACT_ENTITY.value,
                            retrieval_timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                    )

        if not combined and website_url:
            web_items = self.fetch_website_news(
                orgnr=orgnr,
                company_name=company_name,
                website_url=website_url,
                is_parent_group=is_parent_group,
                max_items=4,
            )
            for item in web_items:
                clean_u = item.canonical_url.split("?")[0].rstrip("/")
                if clean_u not in seen_urls:
                    seen_urls.add(clean_u)
                    combined.append(item)

        # 2. Official statutory announcements from Brønnøysundregistrene
        statutory_items = self.fetch_statutory_announcements(
            orgnr=orgnr,
            company_name=company_name,
            max_items=4,
        )
        for s_item in statutory_items:
            clean_u = s_item.canonical_url.split("?")[0].rstrip("/")
            # Allow statutory items alongside web items
            combined.append(s_item)

        # Sort by publication date descending (None at the end)
        def sort_key(item: CompanyNewsItem) -> str:
            return item.publication_date or "0000-00-00"

        combined.sort(key=sort_key, reverse=True)
        return combined[:max_total]
