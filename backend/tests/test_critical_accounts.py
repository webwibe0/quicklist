"""Reported reset lockout, only on the existing pinned disposable PostgreSQL."""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.models.accounts import AuthThrottle
from app.services import authentication
from tests import test_production_accounts as existing

accounts = existing.accounts


@pytest.mark.parametrize("independent_ip_limit", [False, True])
def test_confirmed_reset_clears_only_account_lock(accounts, independent_ip_limit):
    client, engine, mail, settings = accounts
    settings.auth_login_account_limit = 2
    existing.registered(client, mail)
    for _ in range(2):
        assert (
            existing.post(
                client,
                "login",
                {"email": "new@example.test", "password": "Wrong-synthetic-password"},
            ).status_code
            == 401
        )
    locked = existing.post(
        client, "login", {"email": "new@example.test", "password": existing.PASSWORD}
    )
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    account_key = "login:email:" + authentication.digest("new@example.test")
    ip_key = "login:ip:" + authentication.digest("testclient")
    if independent_ip_limit:
        with Session(engine, info={"system_access": True}) as s, s.begin():
            row = s.get(AuthThrottle, ip_key)
            assert row is not None
            row.attempts = settings.auth_login_ip_limit
    assert (
        existing.post(client, "forgot-password", {"email": "new@example.test"}).status_code == 200
    )
    token = existing.link_token(mail)
    assert (
        existing.post(
            client, "reset-password", {"token": token, "password": "New-synthetic-password-5678"}
        ).status_code
        == 200
    )
    with Session(engine, info={"system_access": True}) as s:
        assert s.get(AuthThrottle, account_key) is None
        assert s.get(AuthThrottle, ip_key) is not None
    assert client.get("/api/v1/conversations").status_code == 401
    login = existing.post(
        client, "login", {"email": "new@example.test", "password": "New-synthetic-password-5678"}
    )
    assert login.status_code == (429 if independent_ip_limit else 200)
    if not independent_ip_limit:
        assert (
            existing.post(
                client, "login", {"email": "new@example.test", "password": existing.PASSWORD}
            ).status_code
            == 401
        )


def test_login_retry_after_reports_remaining_window(accounts):
    client, engine, mail, settings = accounts
    existing.registered(client, mail)
    key = "login:email:" + authentication.digest("new@example.test")
    with Session(engine, info={"system_access": True}) as s, s.begin():
        s.add(
            AuthThrottle(
                key=key,
                attempts=settings.auth_login_account_limit,
                expires_at=authentication.now() + timedelta(seconds=17),
            )
        )
    response = existing.post(
        client, "login", {"email": "new@example.test", "password": existing.PASSWORD}
    )
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 17


def test_one_account_short_term_ai_limit_does_not_lock_another(accounts):
    from fastapi.testclient import TestClient

    from app.ai.openai_provider import get_provider
    from app.main import app
    from tests.fakes import FakeLLMProvider

    client, engine, mail, _ = accounts
    existing.registered(client, mail, "first@example.test")
    first_id = client.get("/api/v1/auth/me").json()["id"]
    app.dependency_overrides[get_provider] = lambda: FakeLLMProvider(
        {"Проверка лимита": {"intent": "UNKNOWN"}}
    )
    with Session(engine, info={"system_access": True}) as s, s.begin():
        s.add(AuthThrottle(key="paid:user:" + first_id, attempts=60,
                           expires_at=authentication.now() + timedelta(minutes=15)))

    def message(browser):
        csrf = browser.get("/api/v1/auth/csrf").json()["csrf_token"]
        return browser.post("/api/v1/assistant/messages", json={"message": "Проверка лимита"},
                            headers={"Origin": existing.ORIGIN, "X-CSRF-Token": csrf})

    assert message(client).status_code == 429
    with TestClient(app, base_url=existing.ORIGIN) as second:
        existing.registered(second, mail, "second@example.test")
        assert message(second).status_code == 200
