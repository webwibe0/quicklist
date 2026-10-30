"""Attempt statistics never replace the monetary guard; no real API is used."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from app.ai.paid_limits import LimitedTransport
from app.core.config import Settings
from app.core.spend_budget import BudgetBlocked, SpendBudget


def config(tmp_path, amount=3.95):
    return Settings(
        _env_file=None,
        interactive_budget_path=tmp_path / "interactive.sqlite3",
        interactive_budget_usd=amount,
        interactive_budget_max_calls=0,
    )


def test_interactive_attempt_cap_is_disabled_by_default():
    assert Settings(_env_file=None).interactive_budget_max_calls == 0


def test_more_than_one_thousand_attempts_are_statistics_not_a_lifetime_block(tmp_path):
    budget = SpendBudget(tmp_path / "budget.sqlite3", limit_nanos=10000, max_calls=0)
    for _ in range(1005):
        budget.reserve("nlu", 1)
    assert budget.summary()["calls"] == 1005
    assert budget.summary()["committed_nanos"] == 1005
    reopened = SpendBudget(tmp_path / "budget.sqlite3", limit_nanos=10000, max_calls=0)
    assert reopened.reserve("embedding", 1) == 1006


def test_approved_binding_transition_keeps_old_one_hundred_rows_and_reserves(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = SpendBudget(path, limit_nanos=10000, max_calls=100)
    for _ in range(100):
        old.reserve("nlu", 1)
    with sqlite3.connect(path) as db:
        before = db.execute("select * from calls order by id").fetchall()
        binding = json.loads(db.execute("select value from meta where key='binding'").fetchone()[0])
        assert binding == [10000, 100, 0]
        db.execute("update meta set value=? where key='binding'", (json.dumps([10000, 0, 0]),))
    new = SpendBudget(path, limit_nanos=10000, max_calls=0)
    new.reserve("nlu", 1)
    with sqlite3.connect(path) as db:
        assert db.execute("select * from calls where id<=100 order by id").fetchall() == before
    assert new.summary()["unknown"] == 101
    with pytest.raises(BudgetBlocked, match="BUDGET_BINDING_CHANGED"):
        SpendBudget(path, limit_nanos=10001, max_calls=0)


def test_unlimited_attempts_still_enforce_money_under_parallel_requests(tmp_path):
    b = SpendBudget(tmp_path / "parallel.sqlite3", limit_nanos=5, max_calls=0)

    def reserve(_):
        try:
            b.reserve("nlu", 1)
            return 1
        except BudgetBlocked:
            return 0

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(reserve, range(20))) == 5
    assert b.summary()["committed_nanos"] == 5


def test_one_row_per_actual_nlu_and_embedding_request_and_no_row_for_rejected_request(tmp_path):
    network = []

    def response(request):
        network.append(request.url.path)
        return httpx.Response(
            200,
            request=request,
            json={"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
        )

    transport = LimitedTransport(config(tmp_path), httpx.MockTransport(response))
    for path, data in [
        ("responses", {"model": "gpt-5.6-luna", "max_output_tokens": 512}),
        ("embeddings", {"model": "text-embedding-3-large", "dimensions": 1536, "input": ["test"]}),
    ]:
        transport.handle_request(
            httpx.Request("POST", "https://api.openai.com/v1/" + path, json=data)
        )
    assert transport.budget.summary()["calls"] == len(network) == 2
    with pytest.raises(BudgetBlocked):
        transport.handle_request(
            httpx.Request("POST", "https://api.openai.com/v1/unpriced", json={})
        )
    assert transport.budget.summary()["calls"] == 2


def test_failed_network_keeps_one_unknown_reserve_without_automatic_retry(tmp_path):
    calls = []

    def unavailable(request):
        calls.append(request)
        raise httpx.ConnectError("synthetic", request=request)

    t = LimitedTransport(config(tmp_path), httpx.MockTransport(unavailable))
    with pytest.raises(httpx.ConnectError):
        t.handle_request(
            httpx.Request(
                "POST",
                "https://api.openai.com/v1/responses",
                json={"model": "gpt-5.6-luna", "max_output_tokens": 512},
            )
        )
    assert (
        len(calls) == 1 and t.budget.summary()["calls"] == 1 and t.budget.summary()["unknown"] == 1
    )


def test_budget_refusal_explains_non_renewing_limit_without_network(tmp_path):
    from app.ai.paid_limits import budget_message

    calls = []
    t = LimitedTransport(config(tmp_path, 0), httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(BudgetBlocked) as failure:
        t.handle_request(
            httpx.Request(
                "POST",
                "https://api.openai.com/v1/audio/speech",
                json={"model": "gpt-4o-mini-tts", "input": "test"},
            )
        )
    message = budget_message(failure.value, tts=True)
    assert "бюджет" in message.lower() and "позже" not in message.lower()
    assert calls == [] and t.budget.summary()["calls"] == 0


def test_busy_network_rejects_temporarily_without_reserving_or_blocking_forever(
    tmp_path, monkeypatch
):
    from threading import BoundedSemaphore

    from app.ai import paid_limits

    monkeypatch.setattr(paid_limits, "NETWORK_SLOTS", BoundedSemaphore(1))
    entered, release = Event(), Event()

    def response(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(
            200, request=request, json={"usage": {"input_tokens": 1, "output_tokens": 1}}
        )

    t = LimitedTransport(config(tmp_path), httpx.MockTransport(response))
    request = httpx.Request(
        "POST",
        "https://api.openai.com/v1/responses",
        json={"model": "gpt-5.6-luna", "max_output_tokens": 512},
    )
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(t.handle_request, request)
        try:
            assert entered.wait(5)
            with pytest.raises(BudgetBlocked, match="BUDGET_BUSY"):
                t.handle_request(request)
            assert t.budget.summary()["calls"] == 1
        finally:
            release.set()
        first.result()
    t.handle_request(request)
    assert t.budget.summary()["calls"] == 2
