from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.spend_budget import BudgetBlocked, SpendBudget


def test_reservation_survives_restart_and_unknown_outcome(tmp_path):
    path = tmp_path / "budget.sqlite3"
    budget = SpendBudget(path, limit_nanos=1000, max_calls=10)
    budget.reserve("embedding", 600)
    reopened = SpendBudget(path, limit_nanos=1000, max_calls=10)
    with pytest.raises(BudgetBlocked):
        reopened.reserve("embedding", 401)
    assert reopened.summary()["committed_nanos"] == 600


def test_parallel_reservations_cannot_overrun(tmp_path):
    budget = SpendBudget(tmp_path / "budget.sqlite3", limit_nanos=1000, max_calls=20)

    def reserve(_):
        try:
            budget.reserve("embedding", 300)
            return 1
        except BudgetBlocked:
            return 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    assert budget.summary()["committed_nanos"] == 900


def test_usage_frees_only_verified_unused_reservation_and_cap_is_bound(tmp_path):
    path = tmp_path / "budget.sqlite3"
    budget = SpendBudget(path, limit_nanos=1000, max_calls=2)
    ticket = budget.reserve("embedding", 800)
    budget.finish(ticket, 200, input_tokens=2)
    assert budget.summary()["committed_nanos"] == 200
    with pytest.raises(BudgetBlocked):
        SpendBudget(path, limit_nanos=2000, max_calls=2)
    ticket = budget.reserve("embedding", 800)
    with pytest.raises(BudgetBlocked):
        budget.finish(ticket, 801, input_tokens=3)
    with pytest.raises(BudgetBlocked):
        budget.reserve("embedding", 1)


def test_rate_limit_and_corruption_fail_closed(tmp_path):
    path = tmp_path / "budget.sqlite3"
    budget = SpendBudget(path, limit_nanos=1000, max_calls=10, min_interval=60)
    budget.reserve("embedding", 1)
    with pytest.raises(BudgetBlocked):
        budget.reserve("embedding", 1)
    bad = tmp_path / "corrupt.sqlite3"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(BudgetBlocked):
        SpendBudget(bad, limit_nanos=1000, max_calls=10)
