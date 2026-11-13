"""Automated regression tests for social media discovery and ownership verification."""

from bs4 import BeautifulSoup

from src.validation.ownership import (
    SocialAccountStatus,
    classify_social_account,
    extract_and_verify_social_links,
)


def test_genuine_company_social_account():
    """Verify that a social account matching the researched company name is accepted and verified."""
    result = classify_social_account(
        url="https://www.linkedin.com/company/fjordtech-solutions",
        anchor_context="Følg Fjordtech Solutions på LinkedIn",
        expected_name="FJORDTECH SOLUTIONS AS",
        expected_orgnr="923609016",
        is_parent_group_site=False,
    )

    assert result.status == SocialAccountStatus.VERIFIED
    assert result.ownership_classification == "company_owned_or_official"
    assert result.handle == "fjordtech-solutions"
    assert result.platform == "linkedin"


def test_parent_group_social_account_isolated():
    """Verify social link on parent-group site pointing to parent account is classified as parent_group."""
    # Researched company: Subsidiary "Equinor Energy AS"
    # Social URL: Parent group Equinor account
    result = classify_social_account(
        url="https://www.linkedin.com/company/equinor",
        anchor_context="Equinor official LinkedIn",
        expected_name="EQUINOR ENERGY AS",
        expected_orgnr="985224323",
        is_parent_group_site=True,
    )

    assert result.status == SocialAccountStatus.PARENT_GROUP
    assert result.ownership_classification == "parent_group"
    assert result.handle == "equinor"
    assert "parent group" in result.rejection_reason.lower()


def test_web_agency_account_rejected():
    """Verify web development agency links in footers are detected and rejected."""
    # Example: "Nettside av Maksimer" with Maksimer's LinkedIn
    result = classify_social_account(
        url="https://www.linkedin.com/company/maksimer",
        anchor_context="Nettside utviklet av Maksimer",
        expected_name="NORDIC FISHING AS",
        expected_orgnr="923609016",
    )

    assert result.status == SocialAccountStatus.AGENCY
    assert result.ownership_classification == "third_party"
    assert "agency" in result.rejection_reason.lower()


def test_website_theme_or_template_account_rejected():
    """Verify template author or CMS demonstration social links are detected and rejected."""
    result = classify_social_account(
        url="https://twitter.com/envato",
        anchor_context="Theme by Envato / ThemeForest",
        expected_name="BERGEN CONSULTING AS",
        expected_orgnr="923609016",
    )

    assert result.status == SocialAccountStatus.TEMPLATE
    assert result.ownership_classification == "third_party"
    assert "theme" in result.rejection_reason.lower()


def test_ambiguous_social_account_unverified():
    """Verify account whose handle has no resemblance to company name is marked unverified."""
    result = classify_social_account(
        url="https://www.instagram.com/totally_unrelated_brand_123",
        anchor_context="Instagram",
        expected_name="OSLO BIOTECH AS",
        expected_orgnr="923609016",
    )

    assert result.status == SocialAccountStatus.UNVERIFIED
    assert result.ownership_classification == "unverified"
    assert "could not be definitively verified" in result.rejection_reason.lower()


def test_generic_share_intent_url_invalid():
    """Verify social sharing buttons and generic URLs without account handle are rejected as invalid."""
    share_urls = [
        "https://www.facebook.com/sharer/sharer.php?u=https://example.com",
        "https://www.linkedin.com/shareArticle?mini=true",
        "https://twitter.com/intent/tweet?text=Hello",
        "https://www.instagram.com/",
        "https://www.facebook.com",
    ]

    for s_url in share_urls:
        result = classify_social_account(
            url=s_url,
            anchor_context="Del på sosiale medier",
            expected_name="ACME AS",
            expected_orgnr="923609016",
        )
        assert result.status == SocialAccountStatus.INVALID


def test_extract_and_verify_social_links_from_html():
    """Verify full HTML page extraction accurately filters agency links and retains genuine accounts."""
    html = """
    <html>
      <body>
        <main><h1>Welcome</h1></main>
        <footer>
          <div class="company-social">
            <a href="https://www.linkedin.com/company/fjordtech-solutions">LinkedIn</a>
            <a href="https://www.facebook.com/fjordtechsolutions">Facebook</a>
            <a href="https://facebook.com/sharer">Share on Facebook</a>
          </div>
          <div class="agency-credit">
            <p>Design av Maksimer</p>
            <a href="https://www.linkedin.com/company/maksimer">Maksimer LinkedIn</a>
          </div>
        </footer>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")
    results = extract_and_verify_social_links(
        soup=soup,
        base_url="https://fjordtech.no",
        expected_name="FJORDTECH SOLUTIONS AS",
        expected_orgnr="923609016",
        is_parent_group_site=False,
    )

    # 1. Company LinkedIn -> Verified
    c_linkedin = next(r for r in results if r["handle"] == "fjordtech-solutions")
    assert c_linkedin["status"] == "verified"
    assert c_linkedin["is_verified"] is True

    # 2. Company Facebook -> Verified
    c_fb = next(r for r in results if r["handle"] == "fjordtechsolutions")
    assert c_fb["status"] == "verified"
    assert c_fb["is_verified"] is True

    # 3. Share URL -> Invalid
    share = next(r for r in results if "sharer" in r["url"])
    assert share["status"] == "invalid"
    assert share["is_verified"] is False

    # 4. Agency LinkedIn -> Agency / Rejected
    agency = next(r for r in results if r["handle"] == "maksimer")
    assert agency["status"] == "agency"
    assert agency["is_verified"] is False


def test_social_account_name_match_alone_insufficient_evidence():
    """Verify that a handle matching company name alone without verified website link is not labeled official."""
    # 1. Unlinked / unverified source -> UNVERIFIED
    res_unlinked = classify_social_account(
        url="https://www.linkedin.com/company/equinor",
        anchor_context="Equinor",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
        is_linked_from_official_website=False,
    )
    assert res_unlinked.status == SocialAccountStatus.UNVERIFIED
    assert res_unlinked.is_verified is False
    assert res_unlinked.ownership_classification == "unverified"
    assert "insufficient" in res_unlinked.rejection_reason.lower()
    assert "matches company name" in res_unlinked.evidence.lower()
    assert "not confirmed by an outbound link" in res_unlinked.evidence.lower()

    # 2. Linked from verified official corporate website -> VERIFIED
    res_linked = classify_social_account(
        url="https://www.linkedin.com/company/equinor",
        anchor_context="Follow Equinor on LinkedIn",
        expected_name="EQUINOR ASA",
        expected_orgnr="923609016",
        is_linked_from_official_website=True,
    )
    assert res_linked.status == SocialAccountStatus.VERIFIED
    assert res_linked.is_verified is True
    assert res_linked.ownership_classification == "company_owned_or_official"
    assert "confirmed via outbound link from verified official corporate website" in res_linked.evidence.lower()


def test_roger_aamodt_bygg_rejects_eiendom_facebook_page():
    """Verify organizer regression: ROGER AAMODT BYGG AS rejects Facebook page for Roger Aamodt Eiendom AS."""
    from src.validation.ownership import EntityOwnershipClassification

    result = classify_social_account(
        url="https://www.facebook.com/rogeraamodteiendom/",
        anchor_context="Besøk oss på Facebook",
        expected_name="ROGER AAMODT BYGG AS",
        expected_orgnr="988412988",
        is_linked_from_official_website=True,
    )

    assert result.is_verified is False
    assert result.status == SocialAccountStatus.WRONG_ENTITY
    assert result.classification == EntityOwnershipClassification.REJECTED_WRONG_ENTITY
    assert "eiendom" in result.rejection_reason.lower()
    assert "bygg" in result.rejection_reason.lower()


def test_otis_as_rejects_global_otis_elevator_company_youtube_channel():
    """Verify organizer regression: OTIS AS (Norwegian subsidiary) rejects global Otis Elevator Company YouTube channel."""
    from src.validation.ownership import EntityOwnershipClassification

    result = classify_social_account(
        url="https://www.youtube.com/user/OtisElevatorCo",
        anchor_context="Otis on YouTube",
        expected_name="OTIS AS",
        expected_orgnr="919419105",
        is_linked_from_official_website=True,
    )

    assert result.is_verified is False
    assert result.status == SocialAccountStatus.PARENT_GROUP
    assert result.classification == EntityOwnershipClassification.VERIFIED_OFFICIAL_PARENT
    assert "global parent corporation" in result.rejection_reason.lower()


def test_similar_company_names_isolated():
    """Verify that similar company names with differing sectors are isolated and not conflated."""
    from src.validation.ownership import EntityOwnershipClassification

    # Target: Bergen Maritime Consulting AS. Handle has 'drift'
    res = classify_social_account(
        url="https://www.facebook.com/bergenmaritimedrift",
        anchor_context="Bergen Maritime på Facebook",
        expected_name="BERGEN MARITIME CONSULTING AS",
        expected_orgnr="923609016",
        is_linked_from_official_website=True,
    )
    assert res.is_verified is False
    assert res.classification == EntityOwnershipClassification.REJECTED_WRONG_ENTITY


def test_personal_owner_account_rejected_as_company_official():
    """Verify that an individual owner's personal profile is rejected as official company social profile."""
    from src.validation.ownership import EntityOwnershipClassification

    res = classify_social_account(
        url="https://www.linkedin.com/in/rogeraamodt",
        anchor_context="Roger Aamodt personlig LinkedIn",
        expected_name="ROGER AAMODT BYGG AS",
        expected_orgnr="988412988",
        is_linked_from_official_website=True,
    )
    assert res.is_verified is False
    assert res.classification == EntityOwnershipClassification.UNVERIFIED
    assert "personal profile" in res.rejection_reason.lower()
