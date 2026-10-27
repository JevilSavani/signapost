"""Automated regression tests for news article and publication date validation."""

from src.validation.ownership import (
    NewsValidationStatus,
    parse_publication_date_string,
    validate_news_item,
)


def test_valid_company_article_with_publication_date():
    """Verify that an authentic, dated company announcement is verified with ISO publication date."""
    result = validate_news_item(
        title="Fjordtech vinner kontrakt på 50 millioner kroner med Statnett",
        url="https://fjordtech.no/nyheter/2024/kontrakt-statnett",
        raw_date="15. mars 2024",
        expected_name="FJORDTECH SOLUTIONS AS",
        expected_orgnr="923609016",
        source_url="https://fjordtech.no/nyheter",
    )

    assert result.status == NewsValidationStatus.VERIFIED
    assert result.publication_date == "2024-03-15"
    assert result.canonical_url == "https://fjordtech.no/nyheter/2024/kontrakt-statnett"
    assert "2024-03-15" in result.company_relevance_evidence


def test_product_page_rejected_as_news():
    """Verify product catalog and shop pages misclassified as news are rejected."""
    # Test by URL
    result1 = validate_news_item(
        title="Hydraulisk pumpe modell X-200 for subsea operasjoner",
        url="https://example.com/produkter/hydraulisk-pumpe-x200",
        raw_date="2024-01-10",
        expected_name="ACME AS",
        expected_orgnr="923609016",
        source_url="https://example.com/nyheter",
    )
    assert result1.status == NewsValidationStatus.REJECTED
    assert "product" in result1.rejection_reason.lower()

    # Test by commerce trigger words
    result2 = validate_news_item(
        title="Kjøp nå: Spesialtilbud på industrideler - Begrenset antall",
        url="https://example.com/items/spesialtilbud",
        raw_date="2024-01-10",
        expected_name="ACME AS",
        expected_orgnr="923609016",
        source_url="https://example.com/nyheter",
    )
    assert result2.status == NewsValidationStatus.REJECTED
    assert "product" in result2.rejection_reason.lower()


def test_navigation_labels_rejected_as_news():
    """Verify that menu items, navigation links, and generic labels are rejected."""
    nav_labels = [
        "Nyheter",
        "Siste nytt",
        "Aktuelt",
        "Pressemeldinger",
        "Les mer",
        "Read more",
        "Se alle nyheter",
        "Arkiv",
        "Kategorier",
        "Filter",
        "Om oss",
        "Kontakt oss",
    ]

    for label in nav_labels:
        result = validate_news_item(
            title=label,
            url="https://example.com/nyheter",
            raw_date="2024-03-01",
            expected_name="ACME AS",
            expected_orgnr="923609016",
            source_url="https://example.com/nyheter",
        )
        assert result.status == NewsValidationStatus.REJECTED
        assert "navigation" in result.rejection_reason.lower() or "too short" in result.rejection_reason.lower()


def test_career_job_listing_rejected_as_news():
    """Verify that recruitment postings and career pages are rejected."""
    result = validate_news_item(
        title="Vi søker senior prosjektleder til vårt kontor i Bergen",
        url="https://example.com/karriere/senior-prosjektleder",
        raw_date="2024-02-15",
        expected_name="ACME AS",
        expected_orgnr="923609016",
        source_url="https://example.com/nyheter",
    )
    assert result.status == NewsValidationStatus.REJECTED
    assert "career" in result.rejection_reason.lower() or "recruitment" in result.rejection_reason.lower()


def test_undated_article_marked_unverified():
    """Verify an article missing a verifiable publication date is marked unverified, not fabricated."""
    result = validate_news_item(
        title="Nytt samarbeid om grønn skipsfart lansert",
        url="https://example.com/nyheter/samarbeid-gronn-skipsfart",
        raw_date=None,  # Missing date
        expected_name="ACME AS",
        expected_orgnr="923609016",
        source_url="https://example.com/nyheter",
    )

    assert result.status == NewsValidationStatus.UNVERIFIED
    assert result.publication_date is None
    assert "missing or ambiguous" in result.rejection_reason.lower()


def test_feed_update_timestamp_rejected_as_publication_date():
    """Verify RSS/Atom feed update timestamp is explicitly rejected as article publication date."""
    result = validate_news_item(
        title="Kvartalsrapport for første kvartal ferdigstilt",
        url="https://example.com/nyheter/q1-rapport",
        raw_date="2024-04-30T18:00:00Z",
        expected_name="ACME AS",
        expected_orgnr="923609016",
        source_url="https://example.com/feed.xml",
        is_feed_update_date=True,
    )

    assert result.status == NewsValidationStatus.UNVERIFIED
    assert result.publication_date is None
    assert "feed update timestamp" in result.rejection_reason.lower()


def test_website_last_modified_header_rejected():
    """Verify HTTP Last-Modified / website build date is not used as article publication date."""
    result = validate_news_item(
        title="Årsmøte avholdt i Oslo",
        url="https://example.com/nyheter/aarsmote",
        raw_date="2024-05-15",
        expected_name="ACME AS",
        expected_orgnr="923609016",
        source_url="https://example.com/nyheter",
        is_last_modified_date=True,
    )

    assert result.status == NewsValidationStatus.UNVERIFIED
    assert result.publication_date is None
    assert "site modification" in result.rejection_reason.lower()


def test_parent_group_article_about_different_company_rejected():
    """Verify parent-group news item about a different subsidiary is rejected for the researched company."""
    # Researched company: Subsidiary "Equinor Energy AS"
    # Article headline: Mentions another subsidiary "Equinor Refining AS"
    result = validate_news_item(
        title="Equinor Refining ferdigstiller vedlikehold på Mongstad-raffineriet",
        url="https://equinor.com/news/mongstad-maintenance-2024",
        raw_date="2024-04-10",
        expected_name="EQUINOR ENERGY AS",
        expected_orgnr="985224323",
        source_url="https://equinor.com/news",
        is_parent_group_site=True,
    )

    assert result.status == NewsValidationStatus.REJECTED
    assert "parent-group" in result.rejection_reason.lower() or "subsidiary" in result.rejection_reason.lower()


def test_parse_publication_date_string_formats():
    """Verify accurate extraction across various Norwegian and international date formats."""
    # ISO 8601
    assert parse_publication_date_string("2024-03-15T12:00:00Z") == "2024-03-15"
    assert parse_publication_date_string("2023-11-05") == "2023-11-05"

    # Norwegian numeric: DD.MM.YYYY
    assert parse_publication_date_string("Publisert: 15.03.2024") == "2024-03-15"
    assert parse_publication_date_string("01.12.2023") == "2023-12-01"

    # Norwegian textual: "15. mars 2024"
    assert parse_publication_date_string("15. mars 2024") == "2024-03-15"
    assert parse_publication_date_string("Publisert 2. februar 2024") == "2024-02-02"

    # English textual: "March 15, 2024"
    assert parse_publication_date_string("Published: March 15, 2024") == "2024-03-15"
    assert parse_publication_date_string("15 October 2023") == "2023-10-15"

    # Invalid / Ambiguous
    assert parse_publication_date_string("Last updated recently") is None
    assert parse_publication_date_string("") is None
    assert parse_publication_date_string(None) is None


def test_investor_calendar_with_future_event_dates():
    """Verify investor calendar with future event dates is rejected as news publication date."""
    calendar_events = [
        ("Equinor Q3 2026 press conference", "https://www.equinor.com/news-and-media/q3-2026-press-conference", "2026-10-28"),
        ("Autumn Conference 2026", "https://www.equinor.com/about-us/2026-autumn-conference", "2026-11-24"),
        ("Equinor Q4 2026 and full year results", "https://www.equinor.com/investors/q4-2026-analyst-conference", "2027-02-04"),
    ]

    for title, url, event_date in calendar_events:
        res = validate_news_item(
            title=title,
            url=url,
            raw_date=event_date,
            expected_name="EQUINOR ASA",
            source_url="https://www.equinor.com/news-and-media/events",
            current_date="2026-10-27",
        )
        assert res.status == NewsValidationStatus.REJECTED
        assert res.is_event is True
        assert res.publication_date is None
        assert res.event_date == event_date
        assert "event date" in res.rejection_reason.lower()


def test_future_dated_event_page_incorrectly_classified_as_news():
    """Verify that a future-dated event page is rejected and its date treated as an event date."""
    res = validate_news_item(
        title="Equinor and Aker BP Supplier Day – Harstad 2026",
        url="https://www.equinor.com/news-and-media/equinor-and-aker-bp-supplier-day-harstad-2026",
        raw_date="2026-10-28",
        expected_name="EQUINOR ASA",
        source_url="https://www.equinor.com/news-and-media/events",
        current_date="2026-10-27",
    )
    assert res.status == NewsValidationStatus.REJECTED
    assert res.publication_date is None
    assert res.event_date == "2026-10-28"
    assert res.is_event is True
    assert "event" in res.rejection_reason.lower()


def test_genuine_dated_company_news_article():
    """Verify genuine dated published company news article is verified with its publication date."""
    res = validate_news_item(
        title="Collaborating to develop challenging discoveries",
        url="https://www.equinor.com/news/20261026-collaborating-to-develop-challenging-discoveries",
        raw_date="26 October 2026",
        expected_name="EQUINOR ASA",
        source_url="https://www.equinor.com/news",
        has_explicit_publication_evidence=True,
        current_date="2026-10-27",
    )
    assert res.status == NewsValidationStatus.VERIFIED
    assert res.publication_date == "2026-10-26"
    assert res.is_event is False
    assert res.event_date is None
    assert res.rejection_reason is None


def test_page_with_no_verifiable_publication_date():
    """Verify a page without verifiable publication date is marked unverified, not fabricated."""
    res = validate_news_item(
        title="Equinor establishes new technology centre in Porsgrunn",
        url="https://www.equinor.com/news/new-tech-centre",
        raw_date=None,
        expected_name="EQUINOR ASA",
        source_url="https://www.equinor.com/news",
    )
    assert res.status == NewsValidationStatus.UNVERIFIED
    assert res.publication_date is None
    assert "missing or ambiguous" in res.rejection_reason.lower()


def test_event_date_differing_from_article_publication_date():
    """Verify that when an article has both publication date and scheduled event date, they are distinguished."""
    res = validate_news_item(
        title="Invitation to Autumn Conference 2026",
        url="https://www.equinor.com/news/invitation-autumn-conference-2026",
        raw_date="15. oktober 2026",
        raw_event_date="2026-11-24",
        has_explicit_publication_evidence=True,
        expected_name="EQUINOR ASA",
        source_url="https://www.equinor.com/news",
        current_date="2026-10-27",
    )
    # Publication date is Oct 15, event date is Nov 24
    assert res.status == NewsValidationStatus.VERIFIED
    assert res.publication_date == "2026-10-15"
    assert res.event_date == "2026-11-24"
    assert res.publication_date != res.event_date

