"""Tests for CompanyNewsCollector and JobPostingCollector."""

from unittest.mock import MagicMock
import pytest
from bs4 import BeautifulSoup

from src.collectors.jobs import JobPostingCollector, JobStatus
from src.collectors.news import CompanyNewsCollector
from src.models.company import CompanyNewsItem, JobPosting


def test_job_posting_schema_org_json_ld():
    """Verify that Schema.org JobPosting JSON-LD blocks are parsed into JobPosting models."""
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "JobPosting",
          "title": "Senior Python Backend Engineer",
          "datePosted": "2026-03-01",
          "validThrough": "2026-12-31",
          "jobLocation": {
            "@type": "Place",
            "address": {
              "addressLocality": "Oslo",
              "addressCountry": "NO"
            }
          },
          "url": "/karriere/senior-python-engineer"
        }
        </script>
      </head>
      <body></body>
    </html>
    """
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    mock_client.get.return_value = mock_resp

    collector = JobPostingCollector(client=mock_client)
    postings, has_zero = collector.collect_jobs(
        orgnr="923609016",
        company_name="EQUINOR ASA",
        website_url="https://www.equinor.com",
    )

    assert len(postings) >= 1
    p = postings[0]
    assert p.title == "Senior Python Backend Engineer"
    assert "https://www.equinor.com/karriere/senior-python-engineer" == p.canonical_url
    assert "Oslo" in (p.location or "")
    assert p.status == JobStatus.OPEN
    assert has_zero is False


def test_job_posting_zero_vacancies_detection():
    """Verify that explicit 'ingen ledige stillinger' phrases trigger zero vacancies flag."""
    html = """
    <html>
      <body>
        <h1>Karriere hos oss</h1>
        <p>Vi har for tiden ingen ledige stillinger, men ta gjerne kontakt for en åpen søknad.</p>
      </body>
    </html>
    """
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = html
    mock_client.get.return_value = mock_resp

    collector = JobPostingCollector(client=mock_client)
    postings, has_zero = collector.collect_jobs(
        orgnr="923609016",
        company_name="Test Bygg AS",
        website_url="https://www.testbygg.no",
    )

    assert len(postings) == 0
    assert has_zero is True


def test_job_posting_expired_status():
    """Verify that past application deadlines result in EXPIRED job status."""
    collector = JobPostingCollector()
    status = collector._determine_status("2020-01-01", "Utgått stilling")
    assert status in (JobStatus.EXPIRED, JobStatus.CLOSED)

    status_future = collector._determine_status("2028-12-31", "Aktiv stilling")
    assert status_future == JobStatus.OPEN


def test_news_collector_statutory_announcements():
    """Verify that Brønnøysundregistrene Kunngjøringer rows are parsed into CompanyNewsItem."""
    html = """
    <html>
      <body>
        <table>
          <tr>
            <td>18.08.2026</td>
            <td>Godkjent årsregnskap med bærekraftsrapportering</td>
          </tr>
          <tr>
            <td>02.07.2026</td>
            <td>Kapital | Vedtektsdato</td>
          </tr>
        </table>
      </body>
    </html>
    """
    mock_client = MagicMock()
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.text = html
    mock_client.client.post.return_value = mock_post_resp

    collector = CompanyNewsCollector(client=mock_client)
    items = collector.fetch_statutory_announcements(
        orgnr="923609016",
        company_name="EQUINOR ASA",
        max_items=5,
    )

    assert len(items) == 2
    assert items[0].publication_date == "2026-08-18"
    assert "Godkjent årsregnskap" in items[0].headline
    assert "Brønnøysundregistrene" in items[0].publisher
    assert "https://w2.brreg.no/kunngjoring/hent_en.jsp?orgnr=923609016" == items[0].canonical_url
    assert items[0].ownership_verification in ("verified_exact_entity", "VERIFIED_EXACT_ENTITY")
