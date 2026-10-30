"""Production account contract on a pinned disposable PostgreSQL, with memory-only mail."""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.ai.openai_provider import get_provider
from app.api import account_recovery, auth, routes
from app.core.config import Settings
from app.db import session as database
from app.main import app
from app.models.accounts import AccountToken, AuthSession, User
from app.services import authentication
from app.services.account_mail import MailUnavailable, get_mail
from app.services.indexing_target import verify_indexing_target
from tests.fakes import FakeLLMProvider

ORIGIN = 'https://quicklist.example.test'
PASSWORD = 'Synthetic-password-only-123'


@pytest.fixture
def accounts(monkeypatch):
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Verified disposable PostgreSQL required')
    engine = create_engine(url, hide_parameters=True)
    verify_indexing_target(engine, '7698854534603636773')
    assert engine.url.database == 'stage82_production_readiness_20261028_test'
    with engine.begin() as conn:
        assert conn.scalar(text('SELECT version_num FROM alembic_version')) == '0009'
        conn.exec_driver_sql('TRUNCATE account_tokens, auth_sessions, auth_throttles, google_oauth_states, google_event_links, google_connections, conversations, event_search_documents, events, embedding_cache')
        conn.exec_driver_sql("DELETE FROM users WHERE NOT is_legacy_owner")
    settings = Settings(_env_file=None, database_url=url, app_environment='staging',
        auth_origin=ORIGIN, auth_cookie_secure=True, auth_require_email_verification=True)
    for module in (authentication, auth, account_recovery, routes):
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(engine, expire_on_commit=False))
    mail = []

    class MemoryMail:
        def send(self, recipient, subject, body):
            mail.append((recipient, subject, body))

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_mail] = MemoryMail
    app.dependency_overrides[get_provider] = lambda: FakeLLMProvider({})
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, engine, mail, settings
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()


def post(client, path, body):
    csrf = client.get('/api/v1/auth/csrf').json()['csrf_token']
    return client.post('/api/v1/auth/' + path, json=body,
        headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})


def link_token(mail):
    return re.search(r'#token=([A-Za-z0-9_-]{43})', mail[-1][2]).group(1)


def registered(client, mail, email='new@example.test'):
    response = post(client, 'register', {'email': email, 'password': PASSWORD})
    assert response.status_code == 201 and response.json()['verification_required']
    token = link_token(mail)
    assert post(client, 'verify-email', {'token': token}).status_code == 200
    assert post(client, 'login', {'email': email, 'password': PASSWORD}).status_code == 200
    return token


def test_verified_access_hashed_one_time_token_and_secure_cookie(accounts):
    client, engine, mail, _ = accounts
    response = post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    assert response.status_code == 201
    token = link_token(mail)
    with Session(engine, info={'system_access': True}) as s:
        stored = s.scalars(select(AccountToken)).one()
        assert stored.token_hash != token and len(stored.token_hash) == 64
        assert token not in str(stored.__dict__)
    login = post(client, 'login', {'email': 'new@example.test', 'password': PASSWORD})
    assert login.json()['access_allowed'] is False
    assert client.get('/api/v1/conversations').status_code == 403
    assert '__Host-quicklist_session=' in login.headers['set-cookie']
    assert 'HttpOnly' in login.headers['set-cookie'] and 'Secure' in login.headers['set-cookie']
    assert post(client, 'verify-email', {'token': token}).status_code == 200
    assert client.get('/api/v1/conversations').status_code == 200
    assert post(client, 'verify-email', {'token': token}).status_code == 400


def test_reset_revokes_all_sessions_does_not_verify_email_and_notifies(accounts):
    client, engine, mail, _ = accounts
    registered(client, mail)
    post(client, 'forgot-password', {'email': 'new@example.test'})
    token = link_token(mail)
    result = post(client, 'reset-password', {'token': token, 'password': 'A-new-synthetic-password-789'})
    assert result.status_code == 200
    assert 'пароль изменён' in mail[-1][1]
    assert token not in mail[-1][2] and PASSWORD not in mail[-1][2]
    assert client.get('/api/v1/conversations').status_code == 401
    assert post(client, 'login', {'email': 'new@example.test', 'password': PASSWORD}).status_code == 401
    assert post(client, 'login', {'email': 'new@example.test', 'password': 'A-new-synthetic-password-789'}).status_code == 200
    assert post(client, 'reset-password', {'token': token, 'password': PASSWORD}).status_code == 400
    with Session(engine, info={'system_access': True}) as s:
        user = s.scalar(select(User).where(User.email == 'new@example.test'))
        assert s.scalars(select(AuthSession).where(AuthSession.user_id == user.id)).all().__len__() == 1


@pytest.mark.parametrize('purpose', ['verify', 'reset'])
def test_expired_or_wrong_purpose_tokens_cannot_be_used(accounts, purpose):
    client, engine, mail, _ = accounts
    post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    if purpose == 'reset':
        post(client, 'forgot-password', {'email': 'new@example.test'})
    token = link_token(mail)
    opposite = 'reset-password' if purpose == 'verify' else 'verify-email'
    assert post(client, opposite, {'token': token, 'password': PASSWORD}).status_code == 400
    with Session(engine, info={'system_access': True}) as s, s.begin():
        row = s.scalar(select(AccountToken).where(AccountToken.purpose == purpose))
        row.expires_at = authentication.now() - timedelta(seconds=1)
    endpoint = 'verify-email' if purpose == 'verify' else 'reset-password'
    assert post(client, endpoint, {'token': token, 'password': PASSWORD}).status_code == 400


def test_forgot_and_registration_do_not_enumerate_accounts(accounts):
    client, engine, mail, _ = accounts
    registered(client, mail)
    with engine.begin() as c:
        c.execute(text("UPDATE auth_throttles SET expires_at=clock_timestamp()-interval '1 second' WHERE key LIKE 'register:email:%'"))
    known = post(client, 'forgot-password', {'email': 'new@example.test'})
    unknown = post(client, 'forgot-password', {'email': 'missing@example.test'})
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
    duplicate = post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    fresh = post(client, 'register', {'email': 'second@example.test', 'password': PASSWORD})
    assert duplicate.status_code == fresh.status_code == 201 and duplicate.json() == fresh.json()


def test_resend_shared_cooldown_invalidates_old_link_and_csrf_is_required(accounts):
    client, _, mail, _ = accounts
    post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    old = link_token(mail)
    assert client.post('/api/v1/auth/resend-verification', json={'email': 'new@example.test'}).status_code == 403
    assert post(client, 'resend-verification', {'email': 'new@example.test'}).status_code == 200
    new = link_token(mail)
    assert new != old
    assert post(client, 'resend-verification', {'email': 'new@example.test'}).status_code == 429
    assert post(client, 'verify-email', {'token': old}).status_code == 400
    assert post(client, 'verify-email', {'token': new}).status_code == 200


def test_parallel_token_consumption_has_only_one_winner(accounts):
    client, _, mail, _ = accounts
    post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    token = link_token(mail)
    csrf = client.get('/api/v1/auth/csrf').json()['csrf_token']
    cookies = dict(client.cookies)

    def consume(_):
        with TestClient(app, base_url=ORIGIN) as browser:
            browser.cookies.update(cookies)
            return browser.post('/api/v1/auth/verify-email', json={'token': token},
                headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf}).status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(consume, range(2))) == [200, 400]


def test_unverified_reset_does_not_grant_verified_access(accounts):
    client, _, mail, _ = accounts
    post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    post(client, 'forgot-password', {'email': 'new@example.test'})
    assert post(client, 'reset-password', {'token': link_token(mail), 'password': PASSWORD}).status_code == 200
    assert post(client, 'login', {'email': 'new@example.test', 'password': PASSWORD}).json()['access_allowed'] is False


def test_legacy_owner_not_silently_marked_email_verified(accounts):
    client, engine, _, _ = accounts
    from app.core.passwords import hash_password
    with Session(engine, info={'system_access': True}) as session, session.begin():
        owner = session.scalar(select(User).where(User.is_legacy_owner))
        owner.password_hash = hash_password(PASSWORD)
        email = owner.email
        assert owner.email_verified_at is None
    body = post(client, 'login', {'email': email, 'password': PASSWORD}).json()
    assert body['access_allowed'] and not body['email_verified']


def test_mail_failure_does_not_disclose_account_or_raw_token(accounts):
    client, _, _, _ = accounts

    class Unavailable:
        def send(self, *args):
            raise MailUnavailable('MAIL_DELIVERY_UNCONFIRMED')

    app.dependency_overrides[get_mail] = Unavailable
    first = post(client, 'register', {'email': 'new@example.test', 'password': PASSWORD})
    assert first.status_code == 201 and 'token' not in first.text
    assert post(client, 'forgot-password', {'email': 'new@example.test'}).json() == post(
        client, 'forgot-password', {'email': 'unknown@example.test'}).json()


def test_anonymous_csrf_issuance_is_shared_and_bounded(accounts):
    client, engine, _, settings = accounts
    settings.auth_csrf_ip_limit = 2
    for _ in range(2):
        client.cookies.clear()
        assert client.get('/api/v1/auth/csrf').status_code == 200
    client.cookies.clear()
    assert client.get('/api/v1/auth/csrf').status_code == 429
    with Session(engine, info={'system_access': True}) as session:
        assert len(session.scalars(select(AuthSession)).all()) == 2
