"""Ownership, identity verification, and attribution audit engine.

Provides strict evidentiary validation for:
1. Company website ownership (distinguishing company-owned, parent group, and third-party/agency).
2. Social media account ownership (filtering agency, theme, template, and parent-group accounts).
3. News and publication date validation (filtering navigation labels, product pages, careers, undated feeds).
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.validation.identity import sanitize_orgnr, validate_orgnr

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and Data Classes
# ---------------------------------------------------------------------------

class WebsiteOwnershipStatus(str, Enum):
    """Classification of candidate website ownership."""
    COMPANY_OWNED_OR_OFFICIAL = "company_owned_or_official"
    PARENT_GROUP = "parent_group"
    THIRD_PARTY = "third_party"
    UNVERIFIED = "unverified"


class SocialAccountStatus(str, Enum):
    """Classification of discovered social media accounts."""
    VERIFIED = "verified"
    PARENT_GROUP = "parent_group"
    AGENCY = "agency"
    TEMPLATE = "template"
    UNVERIFIED = "unverified"
    INVALID = "invalid"


class NewsValidationStatus(str, Enum):
    """Classification of discovered news / announcement candidates."""
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


@dataclass
class WebsiteVerificationResult:
    status: WebsiteOwnershipStatus
    is_verified: bool
    confidence: float
    evidence: list[str]
    reasoning: str
    declared_orgnr: str | None = None
    is_parent_group: bool = False


@dataclass
class SocialAccountVerificationResult:
    platform: str
    url: str
    handle: str | None
    status: SocialAccountStatus
    ownership_classification: str
    evidence: str
    rejection_reason: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.status == SocialAccountStatus.VERIFIED



@dataclass
class NewsValidationResult:
    title: str
    url: str
    canonical_url: str | None
    publication_date: str | None
    company_relevance_evidence: str | None
    source_url: str
    status: NewsValidationStatus
    rejection_reason: str | None = None
    event_date: str | None = None
    is_event: bool = False


# ---------------------------------------------------------------------------
# Constants for False-Positive Detection
# ---------------------------------------------------------------------------

THIRD_PARTY_DOMAINS = {
    "facebook.com", "m.facebook.com", "linkedin.com", "instagram.com",
    "twitter.com", "x.com", "youtube.com", "github.com",
    "proff.no", "purehelp.no", "gulesider.no", "1881.no", "brreg.no",
    "finn.no", "wikipedia.org", "wix.com", "wordpress.com", "squarespace.com",
    "enhetsregisteret.no", "regjeringen.no", "altinn.no", "skatteetaten.no",
}

PARKED_DOMAIN_PATTERNS = [
    r"dette\s+domenet\s+er\s+parkert",
    r"domain\s+parked",
    r"buy\s+this\s+domain",
    r"dette\s+domenet\s+er\s+til\s+salgs",
    r"under\s+construction",
    r"parkert\s+hos",
]

PARENT_GROUP_INDICATORS = [
    r"\bkonsern\b",
    r"\bkonsernet\b",
    r"\bgroup\b",
    r"\bholdings?\b",
    r"\bmorselskap\b",
    r"\bvåre\s+selskaper\b",
    r"\bvåre\s+datterselskaper\b",
    r"\bour\s+subsidiaries\b",
    r"\bour\s+brands\b",
    r"\bvåre\s+merkevarer\b",
    r"\bporteføljeselskaper\b",
    r"\binvestor\s+relations\b",
    r"\bkonsernledelse\b",
]

AGENCY_INDICATORS = [
    "maksimer", "bold", "gasta", "innocode", "dekode", "blek", "hyper",
    "knowit", "bouvet", "soprasteria", "creuna", "netlife", "dinamo",
    "anti", "try", "apriil", "redink", "spoon", "webgruppen", "coretrek",
    "mediadigital", "frontkom", "okse", "proisp", "domeneshop", "onecom",
]

THEME_CMS_INDICATORS = [
    "wordpress", "wpengine", "elementor", "squarespace", "wix", "shopify",
    "webflow", "themeforest", "envato", "bootstrap", "woocommerce", "hubspot",
    "mailchimp",
]

AGENCY_CONTEXT_PATTERNS = [
    r"design\s*(?:av|by)",
    r"utviklet\s*(?:av|by)",
    r"nettside\s*(?:av|by)",
    r"laget\s*(?:av|by)",
    r"leverert\s*(?:av|by)",
    r"produced\s*by",
    r"built\s*(?:by|with)",
    r"proudly\s*powered\s*by",
    r"web\s*agency",
    r"webbyrå",
]

NAVIGATION_LABEL_PATTERNS = [
    r"^nyheter$", r"^news$", r"^siste\s*nytt$", r"^aktuelt$",
    r"^pressemeldinger?$", r"^press\s*releases?$", r"^latest\s*news?$",
    r"^les\s*mer$", r"^read\s*more$", r"^se\s*alle\s*nyheter$",
    r"^view\s*all$", r"^arkiv$", r"^archive$", r"^kategorier?$",
    r"^filter$", r"^hjem$", r"^home$", r"^om\s*oss$", r"^about\s*(?:us)?$",
    r"^kontakt(?:\s*oss)?$", r"^contact(?:\s*us)?$", r"^produkter?$",
    r"^tjenester?$", r"^services?$", r"^karriere$", r"^careers?$",
    r"^ledige\s*stillinger?$", r"^meld\s*deg\s*på\s*nyhetsbrev$",
    r"^subscribe$", r"^cookies?$", r"^personvern$", r"^privacy(?:\s*policy)?$",
    r"^søk$", r"^search$", r"^meny$", r"^menu$", r"^logg\s*inn$", r"^login$",
    r"^side\s*\d+$", r"^next$", r"^previous$", r"^neste$", r"^forrige$",
]

PRODUCT_URL_PATTERNS = [
    r"/produkt/", r"/produkter/", r"/product/", r"/products/",
    r"/shop/", r"/butikk/", r"/vare/", r"/varebok/", r"/item/",
]

CAREER_URL_PATTERNS = [
    r"/karriere/", r"/careers/", r"/jobb/", r"/stillinger/", r"/jobs/",
    r"/work-with-us/", r"/ledig-stilling/",
]

EVENT_OR_CALENDAR_URL_PATTERNS = [
    r"/(events?|arrangementer?|kalender|calendar|financial-calendar|finansiell-kalender|investor-calendar|webcasts?|webinarer?|conferences?|konferanser?|presentations?)(/|$|\?)",
    r"/(q[1-4]-20\d\d|full-year-20\d\d)-(press-conference|analyst-conference|results|presentation|webcast)",
    r"/(press-conference|analyst-conference|autumn-conference|annual-conference|supplier-day)",
]

EVENT_TITLE_PATTERNS = [
    r"\b(press\s*conference|pressekonferanse)\b",
    r"\b(analyst\s*conference|analytikerkonferanse|resultatpresentasjon|results\s*presentation)\b",
    r"\b(autumn\s*conference|annual\s*conference|vårkonferanse|høstkonferanse|energidagene|conference\s*20\d\d)\b",
    r"\b(webcast|webinar)\b",
    r"\b(annual\s*general\s*meeting|ordinær\s*generalforsamling|generalforsamling|agm\s*20\d\d)\b",
    r"\b(q[1-4]\s*20\d\d|fourth\s*quarter|first\s*quarter|second\s*quarter|third\s*quarter|full\s*year)\s*(results|press|analyst|conference|presentation|webcast|report)\b",
    r"\b(supplier\s*day|leverandørdag|capital\s*markets\s*day|kapitalmarkedsdag)\b",
    r"\b(save\s*the\s*date|sett\s*av\s*datoen)\b",
    r"\b(invitasjon\s*til|invitation\s*to)\b",
]

BOILERPLATE_SUBSTRING_PATTERNS = [
    r"\bcookies?\b.*\b(accept|consent|experience|preferences|tracking|browser|manage|website)\b",
    r"\b(accept|manage|all|necessary)\s+cookies?\b",
    r"\bvi\s+bruker\s+(informasjonskapsler|cookies)\b",
    r"\binnstillinger\s+for\s+informasjonskapsler\b",
    r"\bcopyright\s+(?:©\s*)?\d{4}\b",
    r"\ball\s+rights\s+reserved\b",
    r"\balle\s+rettigheter\s+(?:forbeholdes|reservert)\b",
    r"\bterms\s+of\s+(?:use|service)\b",
    r"\bvilkår\s+for\s+bruk\b",
    r"\bprivacy\s+policy\b",
    r"\bpersonvernerklæring\b",
    r"\bskip\s+to\s+(?:main\s+)?content\b",
    r"\bdesigned\s+by\b",
    r"\butviklet\s+av\b",
    r"\bpowered\s+by\b",
]

NAVIGATION_TOKENS = {
    "explore", "connect", "site", "menu", "search", "home", "about", "contact",
    "services", "read", "more", "less", "login", "signup", "news", "careers",
    "privacy", "cookies", "terms", "overview", "navigation", "share", "follow",
    "back", "top", "skip", "content", "hjem", "aktuelt", "nyheter", "kontakt",
    "om", "oss", "tjenester", "produkter", "karriere", "meny", "søk",
}


def is_credible_business_description(text: str | None) -> bool:
    """Validate whether candidate text represents a genuine corporate description rather than navigation or boilerplate."""
    if not text:
        return False
    clean = text.strip()
    if len(clean) < 30:
        return False

    words = clean.split()
    if len(words) < 5:
        return False

    clean_lower = clean.lower()

    # Reject if matching known boilerplate regex patterns (cookies, copyright, legal, skip to content)
    for pat in BOILERPLATE_SUBSTRING_PATTERNS:
        if re.search(pat, clean_lower):
            return False

    # Reject if text is predominantly composed of navigation and button words
    # e.g., "Explore Connect Site" or "Home About Services Contact"
    tokens = [re.sub(r"[^\w]", "", w) for w in clean_lower.split() if re.sub(r"[^\w]", "", w)]
    if tokens:
        nav_matches = sum(1 for w in tokens if w in NAVIGATION_TOKENS)
        if (nav_matches / len(tokens)) >= 0.4:
            return False

    # Check for lists of capitalized navigation words without sentence punctuation
    has_punctuation = any(p in clean for p in [".", ",", ";", "!", "?"])
    if not has_punctuation and len(words) <= 8:
        if all(w[0].isupper() for w in words if w):
            return False

    return True


# ---------------------------------------------------------------------------
# Task 2: Website Ownership Classification
# ---------------------------------------------------------------------------

def _clean_company_name_tokens(name: str) -> list[str]:
    """Extract distinctive search tokens from corporate legal name in appearance order."""
    norm = re.sub(r"\b(AS|ASA|ENK|BA|DA|ANS|NUF|HOLDING|GROUP|KONSERN|NORGE|NORWAY)\b", "", name, flags=re.I)
    norm = re.sub(r"[^\w\s]", " ", norm).lower()
    seen = set()
    tokens: list[str] = []
    for w in norm.split():
        if len(w) >= 3 and w not in seen:
            seen.add(w)
            tokens.append(w)
    return tokens


def classify_website_ownership(
    candidate_url: str | None,
    soup: BeautifulSoup | None,
    page_text: str,
    expected_orgnr: str | None,
    expected_name: str | None,
    is_part_of_group: bool | None = None,
) -> WebsiteVerificationResult:
    """Classify candidate domain ownership with strict parent-group and third-party isolation."""
    if not candidate_url:
        return WebsiteVerificationResult(
            status=WebsiteOwnershipStatus.UNVERIFIED,
            is_verified=False,
            confidence=0.0,
            evidence=[],
            reasoning="No candidate URL provided",
        )

    parsed = urlparse(candidate_url)
    domain = parsed.netloc.lower().split(":")[0]
    # Remove 'www.' prefix
    clean_domain = domain[4:] if domain.startswith("www.") else domain

    evidence: list[str] = []

    # 1. Reject third-party directories, social networks, or government registries
    for tp in THIRD_PARTY_DOMAINS:
        if clean_domain == tp or clean_domain.endswith(f".{tp}"):
            return WebsiteVerificationResult(
                status=WebsiteOwnershipStatus.THIRD_PARTY,
                is_verified=False,
                confidence=0.0,
                evidence=[f"Domain '{domain}' matches known directory/platform '{tp}'"],
                reasoning=f"Candidate URL points to external platform/directory '{domain}', not company website",
            )

    # 2. Check for parked or under construction domains
    for pattern in PARKED_DOMAIN_PATTERNS:
        if re.search(pattern, page_text, flags=re.I):
            return WebsiteVerificationResult(
                status=WebsiteOwnershipStatus.THIRD_PARTY,
                is_verified=False,
                confidence=0.0,
                evidence=[f"Page text matched parking indicator: '{pattern}'"],
                reasoning="Domain is parked or under construction without active corporate presence",
            )

    clean_expected_org = sanitize_orgnr(expected_orgnr) if expected_orgnr else None

    # 3. Search for Norwegian Organization Numbers on the page
    found_orgs: set[str] = set()
    org_matches = re.findall(
        r"(?:org(?:anisasjons)?\.?\s*nr\.?|organisasjonsnummer|foretaksregisteret)\s*:?\s*([0-9\s.]{9,15})",
        page_text,
        re.I,
    )
    for raw in org_matches:
        digits = "".join(filter(str.isdigit, raw))
        if len(digits) == 9:
            is_v, clean_o, _ = validate_orgnr(digits)
            if is_v:
                found_orgs.add(clean_o)

    # Also search general 9-digit patterns in footer/header if not found
    if not found_orgs and soup:
        footer = soup.find(["footer", "div"], attrs={"id": re.compile(r"footer", re.I)}) or soup.find(
            "div", attrs={"class": re.compile(r"footer|legal|bottom", re.I)}
        )
        if footer:
            footer_text = footer.get_text()
            f_matches = re.findall(r"\b([0-9]{3}\s*[0-9]{3}\s*[0-9]{3})\b", footer_text)
            for raw in f_matches:
                digits = "".join(filter(str.isdigit, raw))
                if len(digits) == 9:
                    is_v, clean_o, _ = validate_orgnr(digits)
                    if is_v:
                        found_orgs.add(clean_o)

    # Detect parent-group characteristics in page content
    has_parent_group_content = False
    matched_group_indicator = None
    for pattern in PARENT_GROUP_INDICATORS:
        if re.search(pattern, page_text[:5000], flags=re.I):
            has_parent_group_content = True
            matched_group_indicator = pattern
            break

    # 4. Check explicit organization number findings
    if clean_expected_org:
        if clean_expected_org in found_orgs:
            evidence.append(f"Found exact organization number {clean_expected_org} on page")

            # Check if domain belongs to a parent holding/group and researched entity is a subsidiary
            if has_parent_group_content and expected_name:
                expected_is_group = any(w in expected_name.upper() for w in ["HOLDING", "GROUP", "KONSERN", "ASA"])
                if not expected_is_group and is_part_of_group:
                    # The page lists the subsidiary org number within a wider group site
                    return WebsiteVerificationResult(
                        status=WebsiteOwnershipStatus.PARENT_GROUP,
                        is_verified=False,
                        confidence=0.6,
                        evidence=evidence + [f"Detected group indicators: '{matched_group_indicator}'"],
                        reasoning=(
                            f"Domain '{domain}' represents a parent group portal mentioning "
                            f"subsidiary {expected_name} ({clean_expected_org})"
                        ),
                        declared_orgnr=clean_expected_org,
                        is_parent_group=True,
                    )

            return WebsiteVerificationResult(
                status=WebsiteOwnershipStatus.COMPANY_OWNED_OR_OFFICIAL,
                is_verified=True,
                confidence=1.0,
                evidence=evidence,
                reasoning=f"Verified company ownership via exact organization number {clean_expected_org}",
                declared_orgnr=clean_expected_org,
            )

        elif found_orgs:
            # Different organization number found
            other_org = next(iter(found_orgs))
            evidence.append(f"Page explicitly declares different organization number {other_org}")

            if has_parent_group_content or is_part_of_group:
                return WebsiteVerificationResult(
                    status=WebsiteOwnershipStatus.PARENT_GROUP,
                    is_verified=False,
                    confidence=0.7,
                    evidence=evidence,
                    reasoning=(
                        f"Page declares organization number {other_org} representing parent group, "
                        f"not requested company {clean_expected_org}"
                    ),
                    declared_orgnr=other_org,
                    is_parent_group=True,
                )
            else:
                return WebsiteVerificationResult(
                    status=WebsiteOwnershipStatus.THIRD_PARTY,
                    is_verified=False,
                    confidence=0.8,
                    evidence=evidence,
                    reasoning=f"Domain belongs to different entity with organization number {other_org}",
                    declared_orgnr=other_org,
                )

    # 5. Check company name match in title, domain, and metadata
    if expected_name:
        name_tokens = _clean_company_name_tokens(expected_name)
        title = soup.title.get_text().strip().lower() if soup and soup.title else ""
        meta_og = soup.find("meta", attrs={"property": "og:site_name"}) if soup else None
        site_name = (meta_og.get("content", "")).strip().lower() if meta_og else ""

        # Check if domain name directly mirrors company name tokens
        domain_without_tld = clean_domain.split(".")[0].replace("-", "").replace("_", "")
        domain_matches = any(tok in domain_without_tld for tok in name_tokens if len(tok) >= 4)

        title_matches = any(tok in title for tok in name_tokens if len(tok) >= 4)
        site_name_matches = any(tok in site_name for tok in name_tokens if len(tok) >= 4)

        if domain_matches or (title_matches and site_name_matches):
            evidence.append(f"Company name match in domain/title/metadata ({expected_name})")

            # Check if domain is a parent group
            if has_parent_group_content and is_part_of_group:
                expected_is_group = any(w in expected_name.upper() for w in ["HOLDING", "GROUP", "KONSERN", "ASA"])
                if not expected_is_group:
                    return WebsiteVerificationResult(
                        status=WebsiteOwnershipStatus.PARENT_GROUP,
                        is_verified=False,
                        confidence=0.6,
                        evidence=evidence + [f"Group indicator found: '{matched_group_indicator}'"],
                        reasoning=(
                            f"Domain '{domain}' represents parent group with brand '{expected_name}', "
                            "not standalone subsidiary"
                        ),
                        is_parent_group=True,
                    )

            return WebsiteVerificationResult(
                status=WebsiteOwnershipStatus.COMPANY_OWNED_OR_OFFICIAL,
                is_verified=True,
                confidence=0.85,
                evidence=evidence,
                reasoning=f"Verified official company website via domain and title identity matching '{expected_name}'",
            )

    # 6. Insufficient evidence
    return WebsiteVerificationResult(
        status=WebsiteOwnershipStatus.UNVERIFIED,
        is_verified=False,
        confidence=0.3,
        evidence=evidence,
        reasoning=(
            f"Insufficient evidentiary grounding to attribute '{candidate_url}' to "
            f"{expected_name or 'requested company'} ({expected_orgnr or 'N/A'})"
        ),
    )


# ---------------------------------------------------------------------------
# Task 3: Social Media Account Discovery & Verification
# ---------------------------------------------------------------------------

SUPPORTED_SOCIAL_PLATFORMS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "github.com": "github",
}

GENERIC_SOCIAL_PATHS = {
    "", "/", "/share", "/sharer", "/sharer.php", "/sharearticle",
    "/intent", "/intent/tweet", "/plugins", "/plugins/like.php",
    "/events", "/p", "/hashtag", "/explore", "/home",
}


def _extract_account_handle(platform: str, path: str) -> str | None:
    """Extract and normalize candidate account handle from URL path."""
    clean_path = path.strip("/").split("?")[0].split("#")[0]
    if not clean_path:
        return None

    segments = [s for s in clean_path.split("/") if s]
    if not segments:
        return None

    if platform == "linkedin":
        # Paths like /company/acme, /school/acme, /in/person
        if len(segments) >= 2 and segments[0] in ("company", "school", "showcase"):
            return segments[1]
        elif len(segments) == 1 and segments[0] not in ("company", "in", "feed", "jobs"):
            return segments[0]
        return None

    elif platform == "youtube":
        # Paths like /@channel, /c/name, /user/name, /channel/id
        if segments[0].startswith("@"):
            return segments[0][1:]
        elif len(segments) >= 2 and segments[0] in ("c", "user", "channel"):
            return segments[1]
        elif len(segments) == 1:
            return segments[0]
        return None

    elif platform in ("facebook", "instagram", "twitter", "github"):
        first = segments[0]
        # Ignore non-account paths
        if first.lower() in ("p", "reel", "stories", "share", "sharer", "intent", "search", "login"):
            return None
        return first

    return segments[0] if segments else None


def classify_social_account(
    url: str,
    anchor_context: str,
    expected_name: str | None,
    expected_orgnr: str | None,
    is_parent_group_site: bool = False,
    is_linked_from_official_website: bool = True,
) -> SocialAccountVerificationResult:
    """Audit a discovered social media candidate URL against false-positive indicators."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().split(":")[0]
    clean_domain = domain[4:] if domain.startswith("www.") else domain

    platform = None
    for p_domain, p_name in SUPPORTED_SOCIAL_PLATFORMS.items():
        if clean_domain == p_domain or clean_domain.endswith(f".{p_domain}"):
            platform = p_name
            break

    if not platform:
        return SocialAccountVerificationResult(
            platform="unknown",
            url=url,
            handle=None,
            status=SocialAccountStatus.INVALID,
            ownership_classification="third_party",
            evidence="Unsupported domain",
            rejection_reason=f"Domain '{domain}' is not a supported social platform",
        )

    # Check for generic/share URLs
    path_lower = parsed.path.lower().rstrip("/")
    if path_lower in GENERIC_SOCIAL_PATHS or "sharearticle" in path_lower or "intent/tweet" in path_lower:
        return SocialAccountVerificationResult(
            platform=platform,
            url=url,
            handle=None,
            status=SocialAccountStatus.INVALID,
            ownership_classification="third_party",
            evidence="Generic sharing endpoint",
            rejection_reason="URL is a generic sharing button or platform root, not a specific corporate account",
        )

    handle = _extract_account_handle(platform, parsed.path)
    if not handle:
        return SocialAccountVerificationResult(
            platform=platform,
            url=url,
            handle=None,
            status=SocialAccountStatus.INVALID,
            ownership_classification="third_party",
            evidence="No specific handle",
            rejection_reason="URL does not identify a specific account handle",
        )

    handle_lower = handle.lower()
    anchor_lower = anchor_context.lower()

    # 1. Check for web development agency indicators
    is_agency_context = any(re.search(p, anchor_lower) for p in AGENCY_CONTEXT_PATTERNS)
    is_agency_handle = any(agency in handle_lower for agency in AGENCY_INDICATORS)

    if is_agency_context or is_agency_handle:
        return SocialAccountVerificationResult(
            platform=platform,
            url=url,
            handle=handle,
            status=SocialAccountStatus.AGENCY,
            ownership_classification="third_party",
            evidence=f"Agency detection: handle='{handle}', context='{anchor_context[:80]}'",
            rejection_reason=f"Account belongs to web development agency or designer, not researched company",
        )

    # 2. Check for website theme, CMS, or template vendor indicators
    is_theme_handle = any(theme in handle_lower for theme in THEME_CMS_INDICATORS)
    is_theme_context = any(t in anchor_lower for t in ["theme", "template", "powered by", "shopify", "elementor"])

    if is_theme_handle or is_theme_context:
        return SocialAccountVerificationResult(
            platform=platform,
            url=url,
            handle=handle,
            status=SocialAccountStatus.TEMPLATE,
            ownership_classification="third_party",
            evidence=f"Theme/CMS vendor detection: handle='{handle}'",
            rejection_reason=f"Account belongs to website theme or CMS platform, not researched company",
        )

    # 3. Check for parent-group account inheritance
    if is_parent_group_site and expected_name:
        expected_tokens = _clean_company_name_tokens(expected_name)
        if len(expected_tokens) > 1:
            distinguishing_tokens = [tok for tok in expected_tokens[1:] if len(tok) >= 3]
            has_sub_token = any(tok in handle_lower for tok in distinguishing_tokens)
        else:
            has_sub_token = any(tok in handle_lower for tok in expected_tokens if len(tok) >= 4)

        if not has_sub_token:
            return SocialAccountVerificationResult(
                platform=platform,
                url=url,
                handle=handle,
                status=SocialAccountStatus.PARENT_GROUP,
                ownership_classification="parent_group",
                evidence=f"Parent group site inheritance: handle='{handle}' on group domain",
                rejection_reason=f"Account belongs to parent group rather than researched company '{expected_name}'",
            )

    # 4. Identity matching against company name and official source linkage
    if expected_name:
        expected_tokens = _clean_company_name_tokens(expected_name)
        norm_handle = re.sub(r"[_\-.]", "", handle_lower)
        matches_token = any(tok in norm_handle for tok in expected_tokens if len(tok) >= 3)

        if matches_token:
            if is_linked_from_official_website:
                return SocialAccountVerificationResult(
                    platform=platform,
                    url=url,
                    handle=handle,
                    status=SocialAccountStatus.VERIFIED,
                    ownership_classification="company_owned_or_official",
                    evidence=f"Account confirmed via outbound link from verified official corporate website; handle '{handle}' matches company name '{expected_name}'",
                )
            else:
                return SocialAccountVerificationResult(
                    platform=platform,
                    url=url,
                    handle=handle,
                    status=SocialAccountStatus.UNVERIFIED,
                    ownership_classification="unverified",
                    evidence=f"Handle '{handle}' matches company name '{expected_name}', but account is not confirmed by an outbound link from a verified corporate website",
                    rejection_reason="Name match alone without verified corporate website linkage or official registry confirmation is insufficient proof of account ownership",
                )

    # If handle does not clearly match company name, mark unverified
    return SocialAccountVerificationResult(
        platform=platform,
        url=url,
        handle=handle,
        status=SocialAccountStatus.UNVERIFIED,
        ownership_classification="unverified",
        evidence=f"Handle '{handle}' has ambiguous correspondence with company name '{expected_name}'",
        rejection_reason=f"Handle '{handle}' could not be definitively verified as belonging to '{expected_name}'",
    )


def extract_and_verify_social_links(
    soup: BeautifulSoup,
    base_url: str,
    expected_name: str | None,
    expected_orgnr: str | None,
    is_parent_group_site: bool = False,
    is_website_verified: bool = True,
) -> list[dict[str, Any]]:
    """Discover candidate social links from webpage and classify their ownership."""
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        if not raw_href.startswith("http://") and not raw_href.startswith("https://"):
            continue

        parsed = urlparse(raw_href)
        domain = parsed.netloc.lower().split(":")[0]
        clean_domain = domain[4:] if domain.startswith("www.") else domain

        if not any(clean_domain == p or clean_domain.endswith(f".{p}") for p in SUPPORTED_SOCIAL_PLATFORMS):
            continue

        clean_url = raw_href.split("?")[0].rstrip("/")
        if clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)

        # Build context from anchor text, aria-label, title, or parent container
        parent = a.find_parent(["div", "p", "li", "footer"])
        parent_text = parent.get_text(separator=" ", strip=True) if parent else ""
        anchor_text = a.get_text(strip=True)
        aria_label = a.get("aria-label", "")
        title_attr = a.get("title", "")
        combined_context = f"{anchor_text} {aria_label} {title_attr} {parent_text[:120]}"

        classification = classify_social_account(
            url=raw_href,
            anchor_context=combined_context,
            expected_name=expected_name,
            expected_orgnr=expected_orgnr,
            is_parent_group_site=is_parent_group_site,
            is_linked_from_official_website=is_website_verified,
        )

        results.append({
            "platform": classification.platform,
            "url": raw_href,
            "handle": classification.handle,
            "status": classification.status.value,
            "ownership_classification": classification.ownership_classification,
            "evidence": classification.evidence,
            "rejection_reason": classification.rejection_reason,
            "is_verified": classification.status == SocialAccountStatus.VERIFIED,
        })

    return results


# ---------------------------------------------------------------------------
# Task 4: News Item and Publication Date Validation
# ---------------------------------------------------------------------------

NORWEGIAN_MONTHS = {
    "januar": "01", "februar": "02", "mars": "03", "april": "04",
    "mai": "05", "juni": "06", "juli": "07", "august": "08",
    "september": "09", "oktober": "10", "november": "11", "desember": "12",
}

ENGLISH_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def parse_publication_date_string(raw_date_str: str | None) -> str | None:
    """Parse a date string into an authoritative ISO 8601 YYYY-MM-DD date."""
    if not raw_date_str:
        return None

    cleaned = raw_date_str.strip()

    # 1. ISO 8601 format: YYYY-MM-DD (matches standalone dates and ISO timestamps like 2024-03-15T12:00:00Z)
    iso_match = re.search(r"(20[12]\d)-([01]\d)-([0-3]\d)", cleaned)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"

    # 2. Norwegian numeric date: DD.MM.YYYY
    no_num_match = re.search(r"\b([0-3]?\d)\.([01]?\d)\.(20[12]\d)\b", cleaned)
    if no_num_match:
        day = int(no_num_match.group(1))
        month = int(no_num_match.group(2))
        year = int(no_num_match.group(3))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # 3. Norwegian textual date: "15. mars 2024" or "15 mars 2024"
    for m_name, m_num in NORWEGIAN_MONTHS.items():
        pattern = rf"\b([0-3]?\d)\.?\s+{m_name}\s+(20[12]\d)\b"
        m = re.search(pattern, cleaned, re.I)
        if m:
            day = int(m.group(1))
            year = int(m.group(2))
            if 1 <= day <= 31:
                return f"{year:04d}-{m_num}-{day:02d}"

    # 4. English textual date: "March 15, 2024" or "15 March 2024"
    for m_name, m_num in ENGLISH_MONTHS.items():
        pattern1 = rf"\b{m_name}\s+([0-3]?\d),?\s+(20[12]\d)\b"
        m1 = re.search(pattern1, cleaned, re.I)
        if m1:
            day = int(m1.group(1))
            year = int(m1.group(2))
            if 1 <= day <= 31:
                return f"{year:04d}-{m_num}-{day:02d}"

        pattern2 = rf"\b([0-3]?\d)\s+{m_name}\s+(20[12]\d)\b"
        m2 = re.search(pattern2, cleaned, re.I)
        if m2:
            day = int(m2.group(1))
            year = int(m2.group(2))
            if 1 <= day <= 31:
                return f"{year:04d}-{m_num}-{day:02d}"

    return None


def validate_news_item(
    title: str,
    url: str,
    raw_date: str | None = None,
    expected_name: str | None = None,
    expected_orgnr: str | None = None,
    source_url: str = "",
    is_parent_group_site: bool = False,
    is_feed_update_date: bool = False,
    is_last_modified_date: bool = False,
    raw_event_date: str | None = None,
    has_explicit_publication_evidence: bool = False,
    current_date: str | None = None,
) -> NewsValidationResult:
    """Validate that candidate item is an authentic, dated article about the researched company.

    Distinguishes publication_date from event_date, rejects investor calendars/scheduled events,
    and requires explicit evidence for publication dates.
    """
    clean_title = re.sub(r"\s+", " ", title or "").strip()

    # 1. Reject navigation labels
    if not clean_title or len(clean_title) < 8:
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.REJECTED,
            rejection_reason="Title is too short or empty",
        )

    for pattern in NAVIGATION_LABEL_PATTERNS:
        if re.search(pattern, clean_title, re.I):
            return NewsValidationResult(
                title=clean_title,
                url=url,
                canonical_url=url,
                publication_date=None,
                event_date=None,
                is_event=False,
                company_relevance_evidence=None,
                source_url=source_url,
                status=NewsValidationStatus.REJECTED,
                rejection_reason=f"Title '{clean_title}' is a navigation/section label, not an article",
            )

    parsed_url = urlparse(url)
    path_lower = parsed_url.path.lower()
    source_path_lower = urlparse(source_url).path.lower() if source_url else ""

    # 2. Reject product detail pages
    for p_pat in PRODUCT_URL_PATTERNS:
        if re.search(p_pat, path_lower):
            return NewsValidationResult(
                title=clean_title,
                url=url,
                canonical_url=url,
                publication_date=None,
                event_date=None,
                is_event=False,
                company_relevance_evidence=None,
                source_url=source_url,
                status=NewsValidationStatus.REJECTED,
                rejection_reason="URL is a product or e-commerce catalog page",
            )

    if any(term in clean_title.lower() for term in ["kjøp nå", "legg i handlekurv", "pris:", "på lager", "in stock"]):
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.REJECTED,
            rejection_reason="Headline represents a product listing, not news",
        )

    # 3. Reject career / job listings
    for c_pat in CAREER_URL_PATTERNS:
        if re.search(c_pat, path_lower):
            return NewsValidationResult(
                title=clean_title,
                url=url,
                canonical_url=url,
                publication_date=None,
                event_date=None,
                is_event=False,
                company_relevance_evidence=None,
                source_url=source_url,
                status=NewsValidationStatus.REJECTED,
                rejection_reason="URL is a career or job listing page",
            )

    if any(term in clean_title.lower() for term in ["stilling ledig", "vi søker", "søk her", "apply now"]):
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.REJECTED,
            rejection_reason="Headline represents a recruitment posting, not corporate news",
        )

    # 4. Reject listing / index pages
    if path_lower.rstrip("/") in ("/nyheter", "/news", "/aktuelt", "/pressemeldinger", "/arkiv", "/category"):
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.REJECTED,
            rejection_reason="URL is a listing index, not an article page",
        )

    # 5. Detect Investor Calendars, Scheduled Events, Conferences, and Webcasts
    is_event_url = any(re.search(pat, path_lower) for pat in EVENT_OR_CALENDAR_URL_PATTERNS)
    is_event_source = any(re.search(pat, source_path_lower) for pat in EVENT_OR_CALENDAR_URL_PATTERNS)
    is_event_title = any(re.search(pat, clean_title, re.I) for pat in EVENT_TITLE_PATTERNS)
    is_event = is_event_url or is_event_source or is_event_title or bool(raw_event_date)
    extracted_event_date: str | None = None

    if is_event:
        # Extract event date strictly as event_date, NEVER as publication_date
        extracted_event_date = parse_publication_date_string(raw_event_date or raw_date)
        # Check if page also contains a genuine, verifiable published article date
        verified_pub_date = (
            parse_publication_date_string(raw_date)
            if (has_explicit_publication_evidence and raw_date and raw_date != raw_event_date)
            else None
        )

        if not verified_pub_date:
            return NewsValidationResult(
                title=clean_title,
                url=url,
                canonical_url=url,
                publication_date=None,
                event_date=extracted_event_date,
                is_event=True,
                company_relevance_evidence=None,
                source_url=source_url,
                status=NewsValidationStatus.REJECTED,
                rejection_reason=(
                    f"Scheduled event or calendar page detected ('{clean_title}'); "
                    f"event date {extracted_event_date or 'N/A'} cannot be treated as news publication date"
                ),
            )

    # 6. Exclude feed update timestamps or site-wide Last-Modified dates
    if is_feed_update_date:
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.UNVERIFIED,
            rejection_reason="Feed update timestamp cannot be used as article publication date",
        )

    if is_last_modified_date:
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.UNVERIFIED,
            rejection_reason="Site modification header cannot be used as article publication date",
        )

    # 7. Parse and Validate Publication Date
    parsed_date = parse_publication_date_string(raw_date)
    if not parsed_date:
        return NewsValidationResult(
            title=clean_title,
            url=url,
            canonical_url=url,
            publication_date=None,
            event_date=None,
            is_event=False,
            company_relevance_evidence=None,
            source_url=source_url,
            status=NewsValidationStatus.UNVERIFIED,
            rejection_reason="Article publication date is missing or ambiguous",
        )

    # 8. Future-Dated Article Validation
    # Do not reject solely because date is in the future; require explicit evidence that it is a publication date
    ref_date = current_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if parsed_date > ref_date:
        if not has_explicit_publication_evidence:
            return NewsValidationResult(
                title=clean_title,
                url=url,
                canonical_url=url,
                publication_date=None,
                event_date=parsed_date,
                is_event=True,
                company_relevance_evidence=None,
                source_url=source_url,
                status=NewsValidationStatus.UNVERIFIED,
                rejection_reason=(
                    f"Future-dated date '{parsed_date}' requires explicit publication evidence "
                    "to confirm it is genuinely a publication date and not a scheduled event date"
                ),
            )

    # 9. Company Relevance on Parent Group Websites
    if is_parent_group_site and expected_name:
        tokens = _clean_company_name_tokens(expected_name)
        if len(tokens) > 1:
            distinguishing = [tok for tok in tokens[1:] if len(tok) >= 3]
            has_relevance = any(tok in clean_title.lower() for tok in distinguishing)
        else:
            has_relevance = any(tok in clean_title.lower() for tok in tokens if len(tok) >= 4)
        if not has_relevance:
            return NewsValidationResult(
                title=clean_title,
                url=url,
                canonical_url=url,
                publication_date=parsed_date,
                event_date=None,
                is_event=False,
                company_relevance_evidence=None,
                source_url=source_url,
                status=NewsValidationStatus.REJECTED,
                rejection_reason=(
                    f"Article on parent-group site does not specifically mention subsidiary '{expected_name}'"
                ),
            )

    evidence_text = f"Verified publication date ({parsed_date}) on {urlparse(url).netloc}; passed non-event and non-navigation checks"
    if expected_name:
        evidence_text += f" matching company {expected_name}"

    return NewsValidationResult(
        title=clean_title,
        url=url,
        canonical_url=url,
        publication_date=parsed_date,
        event_date=extracted_event_date,
        is_event=is_event,
        company_relevance_evidence=evidence_text,
        source_url=source_url,
        status=NewsValidationStatus.VERIFIED,
    )
