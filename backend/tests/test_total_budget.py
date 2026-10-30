from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.spend_budget import BudgetBlocked, SpendBudget


def config(**values):
    return Settings(_env_file=None, ai_total_budget_usd=4,
                    interactive_budget_usd=3.95, embedding_budget_usd=.05, **values)


def test_partition_rejects_two_valid_caps_exceeding_authorized_total():
    with pytest.raises(ValidationError, match='total AI budget'):
        Settings(_env_file=None, ai_total_budget_usd=4,
                 interactive_budget_usd=4, embedding_budget_usd=.05)


def test_total_cannot_be_raised_above_four():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_total_budget_usd=4.01)


def test_partition_bounds_parallel_calls_and_retains_unknown_reserves(tmp_path):
    settings = config()
    interactive = SpendBudget(tmp_path/'interactive.sqlite3',
        limit_nanos=round(settings.interactive_budget_usd*1e9), max_calls=100)
    worker = SpendBudget(tmp_path/'worker.sqlite3',
        limit_nanos=round(settings.embedding_budget_usd*1e9), max_calls=500)
    interactive.reserve('tts', 2_000_000_000)

    def reserve(index):
        budget, kind, amount = ((interactive, 'nlu', 100_000_000) if index % 2
                                else (worker, 'embedding', 10_000_000))
        try:
            budget.reserve(kind, amount)
        except BudgetBlocked:
            pass

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(reserve, range(100)))
    before = interactive.summary(), worker.summary()
    assert sum(s['committed_nanos'] for s in before) == 3_950_000_000
    reopened = SpendBudget(interactive.path, limit_nanos=3_950_000_000, max_calls=100)
    assert reopened.summary() == before[0]
    assert reopened.summary()['unknown'] == 20
    with pytest.raises(BudgetBlocked):
        reopened.reserve('tts', 1_000_000_000)
