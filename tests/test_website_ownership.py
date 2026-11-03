"""Automated regression tests for company website ownership and parent-group isolation."""

from bs4 import BeautifulSoup
from unittest.mock import MagicMock

from src.collectors.website import WebsiteCollector
from src.extraction.company_facts import extract_profile_from_sources
from src.models.evidence import FactCategory, ProvenanceFact, ResearchOutcome
from src.pipeline.research import research_company_pipeline
from src.validation.ownership import (
    WebsiteOwnershipStatus,
    WebsiteVerificationResult,
    classify_website_ownership,
    is_credible_business_description,
)


def test_correct_company_owned_website():
    """Verify exact matching organization number establishes company-owned website status."""
    html = """
    <html>
      <head><title>Fjordtech Solutions AS - Offisiell nettside</title></head>
      <body>
        <h1>Velkommen til Fjordtech Solutions</h1>
        <footer>
          <p>Fjordtech Solutions AS</p>
          <p>Organisasjonsnummer: 923 609 016 MVA</p>
          <p>Adresse: Strandveien 1, 0150 Oslo</p>
        </footer>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = classify_website_ownership(
        candidate_url="https://fjordtech.no",
        soup=soup,
        page_text=soup.get_text(),
        expected_orgnr="923609016",
        expected_name="FJORDTECH SOLUTIONS AS",
        is_part_of_group=False,
    )

    assert result.status == WebsiteOwnershipStatus.COMPANY_OWNED_OR_OFFICIAL
    assert result.is_verified is True
    assert result.confidence == 1.0
    assert result.declared_orgnr == "923609016"
    assert "exact organization number 923609016" in result.evidence[0]


def test_parent_group_website_detected_and_isolated():
    """Verify that when a website declares the parent group's orgnr, it is classified as parent_group."""
    # Researched company: Subsidiary "Equinor Energy AS" (985224323)
    # Website: Parent group "Equinor ASA" (923609016)
    html = """
    <html>
      <head><title>Equinor Group - Konsernsider og Investor Relations</title></head>
      <body>
        <h1>Equinor Konsern</h1>
        <p>Våre datterselskaper inkluderer Equinor Energy AS og Equinor Refining AS.</p>
        <footer>
          <p>Equinor ASA (Morselskap)</p>
          <p>Org.nr: 923 609 016</p>
        </footer>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = classify_website_ownership(
        candidate_url="https://equinor.com",
        soup=soup,
        page_text=soup.get_text(),
        expected_orgnr="985224323",
        expected_name="EQUINOR ENERGY AS",
        is_part_of_group=True,
    )

    assert result.status == WebsiteOwnershipStatus.PARENT_GROUP
    assert result.is_verified is False
    assert result.is_parent_group is True
    assert result.declared_orgnr == "923609016"
    assert "parent group" in result.reasoning.lower()


def test_unrelated_company_with_similar_name_rejected():
    """Verify domain with a similar name declaring a different unrelated orgnr is classified as third_party."""
    html = """
    <html>
      <head><title>Fjordtech Consulting - Oslo</title></head>
      <body>
        <h1>Fjordtech Consulting DA</h1>
        <footer>
          <p>Org.nr: 984 851 006</p>
        </footer>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = classify_website_ownership(
        candidate_url="https://fjordtech-consulting.no",
        soup=soup,
        page_text=soup.get_text(),
        expected_orgnr="923609016",
        expected_name="FJORDTECH SOLUTIONS AS",
        is_part_of_group=False,
    )

    assert result.status == WebsiteOwnershipStatus.THIRD_PARTY
    assert result.is_verified is False
    assert result.declared_orgnr == "984851006"
    assert "different entity" in result.reasoning.lower()


def test_unverified_website_insufficient_evidence():
    """Verify domain without conclusive evidence or org number is classified as unverified."""
    html = """
    <html>
      <head><title>Vann og Varme - Tjenester</title></head>
      <body>
        <p>Vi tilbyr rørleggertjenester over hele landet.</p>
        <p>Kontakt oss på post@example.com</p>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    result = classify_website_ownership(
        candidate_url="https://vannogvarme.no",
        soup=soup,
        page_text=soup.get_text(),
        expected_orgnr="999999995",
        expected_name="ACME NORWAY AS",
        is_part_of_group=False,
    )

    assert result.status == WebsiteOwnershipStatus.UNVERIFIED
    assert result.is_verified is False
    assert "insufficient" in result.reasoning.lower()


def test_third_party_directory_or_social_domain_rejected():
    """Verify that social platform and directory URLs are immediately rejected as third_party."""
    soup = BeautifulSoup("<html><body>Profile</body></html>", "html.parser")
    for tp_url in ["https://www.facebook.com/acme", "https://proff.no/selskap/acme", "https://gulesider.no/acme"]:
        result = classify_website_ownership(
            candidate_url=tp_url,
            soup=soup,
            page_text="",
            expected_orgnr="923609016",
            expected_name="ACME AS",
        )
        assert result.status == WebsiteOwnershipStatus.THIRD_PARTY
        assert result.is_verified is False


def test_dataset_candidate_contradicted_by_parent_group_website():
    """Verify that when the candidate website turns out to be a parent group site, it is not published as verified company website."""
    mock_reg = MagicMock()
    mock_reg.get_enhet.return_value = {
        "organisasjonsnummer": "985224323",
        "navn": "SUBSIDIARY ENERGY AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "erIKonsern": True,
        "hjemmeside": "https://parentgroup.com",
    }
    mock_reg.get_roller.return_value = None
    mock_reg.get_regnskap.return_value = None
    mock_reg.get_underenheter.return_value = None

    mock_web = MagicMock(spec=WebsiteCollector)
    from src.models.company import OnlinePresence
    mock_web.collect.return_value = OnlinePresence(
        website_url="https://parentgroup.com",
        http_status=200,
        is_identity_verified=False,
        ownership_status="parent_group",
        ownership_reasoning="Website belongs to parent group ParentGroup ASA, not Subsidiary Energy AS",
    )

    envelope = research_company_pipeline(
        "985224323",
        crawl_website=True,
        reg_collector=mock_reg,
        web_collector=mock_web,
        use_dataset=False,
    )

    # Website outcome must be ambiguous or not_available, and NOT published as verified company website
    assert envelope.field_statuses.get("website") == ResearchOutcome.AMBIGUOUS
    assert envelope.company is not None
    assert envelope.company.website is None
    assert "official_website" not in envelope.company.facts


def test_navigation_text_rejected_as_business_description():
    """Verify that navigation labels, boilerplate, and short phrases are rejected as business descriptions."""
    # 1. Navigation items (e.g. Equinor's raw heading text)
    assert not is_credible_business_description("Explore Connect Site")
    assert not is_credible_business_description("Menu Navigation Search Home")
    assert not is_credible_business_description("Home About Contact Services")
    assert not is_credible_business_description("Skip to main content")
    assert not is_credible_business_description("Read more")

    # 2. Cookie banners and legal boilerplate
    assert not is_credible_business_description("We use cookies to ensure you get the best experience on our website. Accept all cookies.")
    assert not is_credible_business_description("Copyright 2026 Equinor ASA. All rights reserved. Terms of use.")
    assert not is_credible_business_description("All rights reserved.")

    # 3. Short generic phrases (< 30 chars or < 5 words)
    assert not is_credible_business_description("Welcome")
    assert not is_credible_business_description("Welcome to our website")
    assert not is_credible_business_description("About us")
    assert not is_credible_business_description(None)
    assert not is_credible_business_description("")

    # 4. Credible business description should pass
    valid_desc = (
        "We are an international energy company headquartered in Norway, "
        "with around 25,000 employees and operations in more than 20 countries worldwide."
    )
    assert is_credible_business_description(valid_desc)


def test_website_collector_prefers_meta_description_over_nav_headings():
    """Verify WebsiteCollector does not concatenate navigation headings into a business summary."""
    from unittest.mock import patch
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <meta name="description" content="We are an international energy company headquartered in Norway.">
      </head>
      <body>
        <nav>
          <h1>Explore</h1>
          <h2>Connect</h2>
          <h2>Site</h2>
        </nav>
        <main>
          <p>Equinor is committed to turning natural resources into energy for people and progress for society.</p>
        </main>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
    mock_resp.url = "https://www.equinor.com"

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client.DEFAULT_USER_AGENT = "SignalpostBot"

    collector = WebsiteCollector(client=mock_client)
    presence = collector.collect(
        "https://www.equinor.com",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
    )

    assert presence.business_summary != "Explore Connect Site"
    assert "energy company" in (presence.business_summary or "")


def test_website_collector_returns_none_when_no_credible_description():
    """Verify WebsiteCollector leaves business_summary as None when only navigation text exists."""
    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Equinor</title>
      </head>
      <body>
        <nav>
          <h1>Explore</h1>
          <h2>Connect</h2>
          <h2>Site</h2>
          <button>Menu</button>
        </nav>
      </body>
    </html>
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
    mock_resp.url = "https://www.equinor.com"

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp
    mock_client.DEFAULT_USER_AGENT = "SignalpostBot"

    collector = WebsiteCollector(client=mock_client)
    presence = collector.collect(
        "https://www.equinor.com",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
    )

    assert presence.business_summary is None


def test_is_credible_business_description_rejects_explore_connect_site():
    """Verify is_credible_business_description rejects Explore Connect Site and navigation patterns."""
    assert not is_credible_business_description("Explore Connect Site")
    assert not is_credible_business_description("Explore Connect Site for all corporate updates and announcements")
    assert not is_credible_business_description("Welcome to our website! All rights reserved.")
    assert is_credible_business_description("We deliver advanced subsea robotics and engineering services across northern Europe.")

