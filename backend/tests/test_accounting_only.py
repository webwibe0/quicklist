"""New owner-authorised policy: ledger history remains, lifetime quotas do not gate calls."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.ai.paid_limits import LimitedTransport
from app.core.config import Settings
from app.core.spend_budget import BudgetBlocked, SpendBudget


def test_explicit_accounting_mode_retains_all_legacy_rows_binding_and_unknown_reserves(tmp_path):
    path = tmp_path / "existing.sqlite3"
    old = SpendBudget(path, limit_nanos=3950000000, max_calls=100)
    for _ in range(3):
        old.reserve("tts", 1000000000)
    for _ in range(97):
        old.reserve("nlu", 1)
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT * FROM calls ORDER BY id").fetchall()
        meta = db.execute("SELECT * FROM meta ORDER BY key").fetchall()
    new = SpendBudget(path, limit_nanos=3950000000, max_calls=100, enforce_limits=False)
    new.reserve("tts", 1000000000)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM calls WHERE id<=100 ORDER BY id").fetchall() == rows
        assert db.execute("SELECT * FROM meta ORDER BY key").fetchall() == meta
    reopened = SpendBudget(path, limit_nanos=3950000000, max_calls=100, enforce_limits=False)
    assert reopened.reserve("nlu", 1) == 102
    assert reopened.summary()["limit_nanos"] is None
    assert reopened.summary()["unknown"] == 102


def test_accounting_overrun_is_recorded_without_freezing_next_user(tmp_path):
    b = SpendBudget(tmp_path / "usage.sqlite3", limit_nanos=1, max_calls=1, enforce_limits=False)
    ticket = b.reserve("nlu", 1)
    b.finish(ticket, 2, input_tokens=1)
    assert b.reserve("nlu", 1) == 2
    assert not b.summary()["frozen"]
    with pytest.raises(BudgetBlocked, match="INVALID_TICKET"):
        b.finish(ticket, 2)
    with sqlite3.connect(b.path) as db:
        assert db.execute("SELECT actual,state FROM calls WHERE id=?", (ticket,)).fetchone() == (
            2,
            "overrun",
        )


def test_accounting_mode_does_not_disable_rate_limits_or_storage_guards(tmp_path):
    b = SpendBudget(
        tmp_path / "rate.sqlite3", limit_nanos=0, max_calls=1, min_interval=30, enforce_limits=False
    )
    b.reserve("embedding", 1)
    with pytest.raises(BudgetBlocked, match="BUDGET_RATE_LIMIT"):
        b.reserve("embedding", 1)
    path = tmp_path / "bad.sqlite3"
    path.write_bytes(b"not a database")
    with pytest.raises(BudgetBlocked, match="BUDGET_STORAGE_UNAVAILABLE"):
        SpendBudget(path, limit_nanos=0, max_calls=0, enforce_limits=False)


def test_parallel_accounting_records_each_request_once(tmp_path):
    b = SpendBudget(tmp_path / "parallel.sqlite3", limit_nanos=0, max_calls=1, enforce_limits=False)
    with ThreadPoolExecutor(6) as pool:
        ids = list(pool.map(lambda _: b.reserve("nlu", 1), range(30)))
    assert len(set(ids)) == 30 and b.summary()["calls"] == 30


def test_transport_accounting_tts_crosses_former_cap_without_rewriting_prior_rows(tmp_path):
    path = tmp_path / "interactive.sqlite3"
    SpendBudget(path, limit_nanos=0, max_calls=1)
    s = Settings(
        _env_file=None,
        ai_usage_mode="accounting_only",
        interactive_budget_usd=0,
        interactive_budget_max_calls=1,
        interactive_budget_path=path,
    )
    network = []

    def response(request):
        network.append(request)
        return httpx.Response(200, request=request, content=b"fake-mp3")

    t = LimitedTransport(s, httpx.MockTransport(response))
    for _ in range(2):
        t.handle_request(
            httpx.Request(
                "POST",
                "https://api.openai.com/v1/audio/speech",
                json={"model": "gpt-4o-mini-tts", "input": "Synthetic QA"},
            )
        )
    assert len(network) == 2 and t.budget.summary()["calls"] == 2
    assert t.budget.summary()["unknown"] == 2  # No invented token usage for binary audio.
    assert json.loads(
        sqlite3.connect(path).execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0]
    ) == [0, 1, 0]


def test_default_capped_mode_still_fails_closed_for_new_local_copies(tmp_path):
    assert Settings(_env_file=None).ai_usage_mode == "capped"
    b = SpendBudget(tmp_path / "default.sqlite3", limit_nanos=0, max_calls=0)
    with pytest.raises(BudgetBlocked, match="BUDGET_EXHAUSTED"):
        b.reserve("nlu", 1)


@pytest.mark.parametrize("path,payload,response,kind", [
    ("/responses", {"model": "gpt-5.6-luna", "max_output_tokens": 512},
     {"usage": {"input_tokens": 8, "output_tokens": 2}}, "nlu"),
    ("/embeddings", {"model": "text-embedding-3-large", "dimensions": 1536, "input": ["Synthetic QA"]},
     {"usage": {"total_tokens": 3}}, "embedding"),
])
def test_accounting_transport_allows_nlu_and_query_embedding_past_legacy_money_and_count(
    tmp_path, path, payload, response, kind,
):
    s = Settings(_env_file=None, ai_usage_mode="accounting_only", interactive_budget_usd=0,
                 interactive_budget_max_calls=1, interactive_budget_path=tmp_path / "usage.sqlite3")
    transport = LimitedTransport(s, httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    for _ in range(2):
        assert transport.handle_request(httpx.Request("POST", "https://api.openai.com/v1" + path,
                                                     json=payload)).status_code == 200
    with sqlite3.connect(transport.budget.path) as db:
        assert db.execute("SELECT kind,state FROM calls").fetchall() == [(kind, "complete")] * 2
    transport.close()


def test_accounting_worker_lifetime_count_and_money_are_not_enforced(tmp_path, monkeypatch):
    # Retain the real minimum-interval check, advance the test clock rather than disabling it.
    clock = [1000.0]
    monkeypatch.setattr("app.core.spend_budget.time.time", lambda: clock[0])
    worker = SpendBudget(tmp_path / "worker.sqlite3", limit_nanos=1, max_calls=500,
                         min_interval=30, enforce_limits=False)
    for _ in range(501):
        worker.finish(worker.reserve("embedding", 2), 2)
        clock[0] += 31
    assert worker.summary()["calls"] == 501


def test_accounting_config_retains_separate_ledgers_without_sum_cap(tmp_path):
    s = Settings(
        _env_file=None,
        ai_usage_mode="accounting_only",
        ai_total_budget_usd=0,
        interactive_budget_path=tmp_path / "a",
        embedding_budget_path=tmp_path / "b",
    )
    assert s.ai_usage_mode == "accounting_only"
    with pytest.raises(ValueError, match="distinct ledger"):
        Settings(
            _env_file=None,
            ai_usage_mode="accounting_only",
            ai_total_budget_usd=0,
            interactive_budget_path=tmp_path / "a",
            embedding_budget_path=tmp_path / "a",
        )
