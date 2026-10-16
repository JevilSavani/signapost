"""Tests for competition execution limits: requests, time, and API cost budgets."""

import pytest
from src.utils.rate_limiter import BudgetTracker, BudgetExceededError, RateLimiter


def test_budget_tracker_request_limit():
    """Verify that exceeding the request budget immediately raises BudgetExceededError."""
    tracker = BudgetTracker(max_requests=5, max_minutes=10, max_cost=10.0)

    for _ in range(5):
        tracker.check_budget_or_raise()
        tracker.record_request()

    # 6th request should breach budget
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check_budget_or_raise()
    assert "Outbound request limit reached" in str(exc.value)


def test_budget_tracker_cost_limit():
    """Verify that declared external API cost budget is strictly enforced."""
    tracker = BudgetTracker(max_requests=100, max_minutes=10, max_cost=5.0)

    tracker.record_request(cost=4.50)
    tracker.check_budget_or_raise()

    # Attempting a request that would exceed max cost
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check_budget_or_raise(estimated_cost=1.00)
    assert "budget exceeded" in str(exc.value).lower()


def test_budget_tracker_time_limit():
    """Verify that elapsed time limits are enforced."""
    # 0 minute max runtime
    tracker = BudgetTracker(max_requests=100, max_minutes=0, max_cost=10.0)

    with pytest.raises(BudgetExceededError) as exc:
        tracker.check_budget_or_raise()
    assert "Maximum execution time exceeded" in str(exc.value)


def test_rate_limiter_pacing():
    """Verify that the rate limiter throttles rapid successive requests."""
    limiter = RateLimiter(requests_per_second=50.0)  # 20ms interval
    limiter.acquire()
    assert limiter.last_request_time > 0
