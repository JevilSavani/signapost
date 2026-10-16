"""Rate limiter and strict execution budget tracking for Signalpost."""

import time
import threading
from datetime import datetime, timezone
from src.config import settings


class BudgetExceededError(RuntimeError):
    """Raised when an outbound request would breach competition constraints."""
    pass


class BudgetTracker:
    """Thread-safe tracker enforcing execution limits:

    - Maximum 2,000 outbound requests
    - Maximum 45 minutes runtime
    - Maximum $10 declared external API cost
    """

    def __init__(
        self,
        max_requests: int = settings.max_outbound_requests,
        max_minutes: int = settings.max_execution_minutes,
        max_cost: float = settings.declared_max_api_cost,
    ) -> None:
        self.max_requests = max_requests
        self.max_seconds = max_minutes * 60.0
        self.max_cost = max_cost

        self._lock = threading.Lock()
        self.start_time = time.time()
        self.total_requests = 0
        self.cached_hits = 0
        self.total_cost = 0.0
        self.failed_requests = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def remaining_requests(self) -> int:
        with self._lock:
            return max(0, self.max_requests - self.total_requests)

    def check_budget_or_raise(self, estimated_cost: float = 0.0) -> None:
        """Verify that making another request complies with all competition constraints."""
        with self._lock:
            if self.total_requests >= self.max_requests:
                raise BudgetExceededError(
                    f"Outbound request limit reached: {self.total_requests}/{self.max_requests}"
                )

            if self.elapsed_seconds >= self.max_seconds:
                raise BudgetExceededError(
                    f"Maximum execution time exceeded: {self.elapsed_seconds:.1f}s >= {self.max_seconds:.1f}s"
                )

            if (self.total_cost + estimated_cost) > self.max_cost:
                raise BudgetExceededError(
                    f"External API budget exceeded: ${self.total_cost + estimated_cost:.2f} > ${self.max_cost:.2f}"
                )

    def record_request(self, cost: float = 0.0, is_success: bool = True) -> None:
        """Register a completed outbound network request."""
        with self._lock:
            self.total_requests += 1
            self.total_cost += cost
            if not is_success:
                self.failed_requests += 1

    def record_cache_hit(self) -> None:
        """Register a request served locally without consuming external outbound budget."""
        with self._lock:
            self.cached_hits += 1

    def get_summary(self) -> dict:
        """Produce a clean summary of current resource consumption."""
        with self._lock:
            return {
                "outbound_requests": self.total_requests,
                "max_requests": self.max_requests,
                "requests_remaining": max(0, self.max_requests - self.total_requests),
                "cache_hits": self.cached_hits,
                "failed_requests": self.failed_requests,
                "declared_cost_usd": round(self.total_cost, 4),
                "max_cost_usd": self.max_cost,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
                "max_seconds": self.max_seconds,
            }


class RateLimiter:
    """Token-bucket style rate limiter to pace outbound network requests."""

    def __init__(self, requests_per_second: float = settings.rate_limit_rps) -> None:
        self.rate = requests_per_second
        self.min_interval = 1.0 / max(0.1, requests_per_second)
        self.last_request_time = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until the minimum interval between requests has passed."""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_request_time
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last_request_time = time.time()


# Singleton instances
global_budget = BudgetTracker()
global_rate_limiter = RateLimiter()
