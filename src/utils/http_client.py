"""Robust, bounded HTTP client with local SQLite caching and retry backoff."""

import hashlib
import json
import logging
import sqlite3
import time
from typing import Any
import httpx

from src.config import settings
from src.utils.rate_limiter import global_budget, global_rate_limiter, BudgetExceededError

logger = logging.getLogger(__name__)


class CachedHttpClient:
    """HTTP client wrapping httpx with caching, rate limiting, and hard budget limits."""

    DEFAULT_USER_AGENT = (
        "Signalpost-Company-Intelligence-Agent/1.0 (+https://builderr.ai; contact: submit@builderr.ai)"
    )

    def __init__(
        self,
        cache_db_path: str | None = None,
        timeout: float = settings.request_timeout_seconds,
    ) -> None:
        self.timeout = timeout
        self.cache_db_path = cache_db_path or str(settings.database_path)
        self._init_cache_table()
        self.client = httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.DEFAULT_USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    def _init_cache_table(self) -> None:
        """Create http_cache table in SQLite database."""
        with sqlite3.connect(self.cache_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS http_cache (
                    url_hash TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    headers_json TEXT,
                    body TEXT NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ttl_seconds INTEGER DEFAULT 86400
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_http_cache_url ON http_cache(url)")

    def _get_cache(self, url: str) -> dict[str, Any] | None:
        """Fetch unexpired cached response if available."""
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.cache_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT status_code, headers_json, body, strftime('%s', 'now') - strftime('%s', cached_at) as age, ttl_seconds
                FROM http_cache
                WHERE url_hash = ?
                """,
                (url_hash,),
            )
            row = cursor.fetchone()
            if row:
                status_code, headers_json, body, age, ttl = row
                if age is not None and age < ttl:
                    return {
                        "status_code": status_code,
                        "headers": json.loads(headers_json) if headers_json else {},
                        "body": body,
                        "is_cached": True,
                    }
        return None

    def _set_cache(
        self, url: str, status_code: int, headers: dict[str, str], body: str, ttl_seconds: int = 86400
    ) -> None:
        """Save successful response to cache."""
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        try:
            with sqlite3.connect(self.cache_db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO http_cache (url_hash, url, status_code, headers_json, body, ttl_seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (url_hash, url, status_code, json.dumps(dict(headers)), body, ttl_seconds),
                )
        except Exception as e:
            logger.warning("Failed to store cache for %s: %s", url, e)

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        max_retries: int = 3,
        ttl_seconds: int = 86400,
    ) -> httpx.Response:
        """Perform a budgeted GET request with caching, backoff, and rate pacing."""
        # Prepare full request URL for cache key
        req = self.client.build_request("GET", url, params=params, headers=headers)
        full_url = str(req.url)

        if use_cache:
            cached = self._get_cache(full_url)
            if cached and cached["status_code"] in (200, 404):
                global_budget.record_cache_hit()
                # Synthesize httpx.Response from cache
                return httpx.Response(
                    status_code=cached["status_code"],
                    headers=cached["headers"],
                    text=cached["body"],
                    request=req,
                )

        # Enforce budget limits before making an outbound network request
        global_budget.check_budget_or_raise()
        global_rate_limiter.acquire()

        backoff = 1.0
        last_exception = None

        for attempt in range(max_retries):
            try:
                response = self.client.send(req)
                is_success = response.status_code < 400 or response.status_code == 404
                global_budget.record_request(cost=0.0, is_success=is_success)

                # Retry on server overload or rate limiting
                if response.status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue

                if use_cache and response.status_code in (200, 404):
                    self._set_cache(
                        full_url,
                        response.status_code,
                        dict(response.headers),
                        response.text,
                        ttl_seconds=ttl_seconds,
                    )

                return response

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                last_exception = exc
                if attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 2.0
                else:
                    global_budget.record_request(cost=0.0, is_success=False)
                    raise

        if last_exception:
            raise last_exception
        raise httpx.RequestError(f"Failed to fetch {full_url} after {max_retries} attempts")

    def close(self) -> None:
        self.client.close()


# Shared HTTP client singleton
http_client = CachedHttpClient()
