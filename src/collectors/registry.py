"""Official Norwegian Brønnøysund Register Centre API connector.

Interacts with:
- Enhetsregisteret (legal entities, foundation dates, NACE codes, addresses, status)
- Roller (official board members, CEO, auditor)
- Regnskapsregisteret (audited financial statements, revenue, operating profit)
"""

import logging
from typing import Any
from src.config import settings
from src.utils.http_client import http_client
from src.validation.identity import parse_and_verify_orgnr

logger = logging.getLogger(__name__)


class RegistryCollector:
    """Connects to the official open APIs at data.brreg.no."""

    def __init__(self, client=http_client) -> None:
        self.client = client
        self.enheter_base_url = settings.brreg_enheter_url.rstrip("/")
        self.regnskap_base_url = settings.brreg_regnskap_url.rstrip("/")

    def get_enhet(self, orgnr: str) -> dict[str, Any] | None:
        """Fetch primary legal entity data from Enhetsregisteret."""
        clean_orgnr = parse_and_verify_orgnr(orgnr)
        url = f"{self.enheter_base_url}/{clean_orgnr}"

        resp = self.client.get(url, use_cache=True)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            logger.info("Company %s not found in Enhetsregisteret", clean_orgnr)
            return None
        else:
            resp.raise_for_status()
            return None

    def get_roller(self, orgnr: str) -> dict[str, Any] | None:
        """Fetch official registered roles (board, CEO, auditor)."""
        clean_orgnr = parse_and_verify_orgnr(orgnr)
        url = f"{self.enheter_base_url}/{clean_orgnr}/roller"

        resp = self.client.get(url, use_cache=True)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            return None
        else:
            logger.warning("Roles API returned status %s for %s", resp.status_code, clean_orgnr)
            return None

    def get_regnskap(self, orgnr: str) -> list[dict[str, Any]] | None:
        """Fetch latest audited annual accounting filings from Regnskapsregisteret."""
        clean_orgnr = parse_and_verify_orgnr(orgnr)
        url = f"{self.regnskap_base_url}/{clean_orgnr}"

        resp = self.client.get(url, use_cache=True)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return [data]
            return None
        elif resp.status_code == 404:
            return None
        else:
            logger.warning("Regnskap API returned status %s for %s", resp.status_code, clean_orgnr)
            return None

    def list_companies(
        self, organisasjonsform: str = "AS", page: int = 0, size: int = 100
    ) -> list[dict[str, Any]]:
        """Query official registry page of legal entities.

        Allows fetching batches of active Norwegian companies directly from the official registry.
        """
        url = self.enheter_base_url
        params = {"organisasjonsform": organisasjonsform, "page": page, "size": size}
        resp = self.client.get(url, params=params, use_cache=True)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("_embedded", {}).get("enheter", [])
        else:
            resp.raise_for_status()
            return []
