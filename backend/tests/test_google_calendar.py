"""Critical real-PG + simulated Google checks. No live Google/OpenAI request."""

import base64
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.ai.openai_provider import get_provider
from app.api import google_calendar as api
from app.api import routes
from app.core.config import Settings
from app.db import session as database
from app.db.ownership import bind_owner
from app.main import app
from app.models.entities import Event
from app.models.google_calendar import GoogleConnection, GoogleEventLink, GoogleOAuthState
from app.services import authentication
from app.services.calendar_outbox import enqueue, event_payload
from app.services.google_calendar import GoogleCalendarService
from app.services.google_client import CalendarError, GoogleClient, TokenCipher
from tests.fakes import FakeLLMProvider

ORIGIN = "http://127.0.0.1:3001"
PASSWORD = "synthetic-account-password-42"
PREFIX = "/api/v1/calendar/google"


class FakeGoogle:
    def __init__(self):
        self.events, self.calls = {}, []
        self.fail = False
        self.timeout_after_insert = False
        self.refreshes = 0
        self.revoked = False
        self.on_call = None

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if self.on_call:
            callback, self.on_call = self.on_call, None
            callback()
        path = request.url.path
        if path == "/token":
            data = parse_qs(request.content.decode())
            if data["grant_type"][0] == "refresh_token":
                if self.revoked:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.refreshes += 1
                code = data["refresh_token"][0].removeprefix("refresh-")
            else:
                code = data["code"][0]
                assert data.get("code_verifier") and data["redirect_uri"][0].endswith("/callback")
            return httpx.Response(
                200,
                json={
                    "access_token": "access-" + code,
                    "refresh_token": "refresh-" + code,
                    "expires_in": 3600,
                },
            )
        if path.endswith("/userinfo"):
            subject = request.headers["authorization"].removeprefix("Bearer access-")
            return httpx.Response(
                200,
                json={"sub": subject, "email": subject + "@example.test", "email_verified": True},
            )
        if path.endswith("/revoke"):
            return httpx.Response(200)
        if "/calendar/v3/" in path:
            if self.fail:
                return httpx.Response(503, json={"error": "synthetic outage"})
            subject = request.headers["authorization"]
            if path.endswith("/events"):
                body = json.loads(request.content)
                key = (subject, body["id"])
                if key in self.events:
                    return httpx.Response(409)
                self.events[key] = body | {"etag": "etag-" + uuid4().hex}
                if self.timeout_after_insert:
                    self.timeout_after_insert = False
                    raise httpx.ReadTimeout("synthetic timeout", request=request)
                return httpx.Response(201, json=self.events[key])
            remote_id = path.rsplit("/", 1)[-1]
            key = (subject, remote_id)
            if key not in self.events:
                return httpx.Response(404)
            if request.method == "GET":
                return httpx.Response(200, json=self.events[key])
            assert request.headers["if-match"] == self.events[key]["etag"]
            if request.method == "DELETE":
                del self.events[key]
                return httpx.Response(204)
            if request.method == "PATCH":
                self.events[key].update(json.loads(request.content))
                self.events[key]["etag"] = "etag-" + uuid4().hex
                return httpx.Response(200, json=self.events[key])
        raise AssertionError("Unexpected simulated Google operation")


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("STAGE9_TEST_DATABASE_URL")
    if not url:
        pytest.skip("STAGE9_TEST_DATABASE_URL required")
    value = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 3})
    assert value.url.host == "127.0.0.1" and value.url.port == 55434
    assert value.url.database == "stage82_stage9_calendar_20261027_test"
    with value.connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert (
            str(c.scalar(text("SELECT system_identifier FROM pg_control_system()")))
            == "7698854534603636773"
        )
        assert (
            c.scalar(
                text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"
                )
            )
            == "QUICKLIST_STAGE82_DISPOSABLE"
        )
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
    yield value
    value.dispose()


@pytest.fixture
def setup(engine, monkeypatch):
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    settings = Settings(
        _env_file=None,
        database_url=os.environ["STAGE9_TEST_DATABASE_URL"],
        auth_origin=ORIGIN,
        google_calendar_enabled=True,
        google_client_id="synthetic-client",
        google_client_secret="synthetic-client-secret",
        google_token_key=base64.urlsafe_b64encode(b"x" * 32).decode(),
        google_redirect_uri="http://127.0.0.1:8001/api/v1/calendar/google/callback",
    )
    for module in (api, routes, authentication):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    fake = FakeGoogle()
    google = GoogleClient(settings, transport=httpx.MockTransport(fake))
    monkeypatch.setattr(google, "close", lambda: None)
    monkeypatch.setattr(api, "GoogleClient", lambda settings: google)
    nlu = FakeLLMProvider({})
    old = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    app.dependency_overrides[api.get_google_client] = lambda: google
    app.dependency_overrides[get_provider] = lambda: nlu
    service = GoogleCalendarService(sessions, google, settings)
    clients = []

    def actor():
        client = TestClient(app, client=("synthetic-" + uuid4().hex, 50000))
        client.__enter__()
        clients.append(client)
        assert client.get("/api/v1/auth/csrf").status_code == 200
        response = client.post(
            "/api/v1/auth/register",
            headers=headers(client),
            json={"email": uuid4().hex + "@example.test", "password": PASSWORD},
        )
        assert response.status_code == 201
        return client, UUID(response.json()["id"])

    yield SimpleNamespace(
        actor=actor,
        service=service,
        fake=fake,
        google=google,
        sessions=sessions,
        settings=settings,
        nlu=nlu,
        engine=engine,
    )
    for client in clients:
        client.__exit__(None, None, None)
    google.http.close()
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("quicklist_csrf", "")}


def connect(value, client, code=None):
    code = code or uuid4().hex
    response = client.post(PREFIX + "/connect", headers=headers(client))
    assert response.status_code == 200
    state = parse_qs(httpx.URL(response.json()["authorization_url"]).query.decode())["state"][0]
    callback = client.get(
        PREFIX + "/callback", params={"state": state, "code": code}, follow_redirects=False
    )
    assert callback.status_code == 303 and callback.headers["location"].endswith(
        "calendar=connected"
    )
    assert callback.headers["referrer-policy"] == "no-referrer"
    return state, code


def enable(client):
    response = client.post(PREFIX + "/sync", headers=headers(client), json={"enabled": True})
    assert response.status_code == 200


def event(value, owner, **kwargs):
    with value.sessions() as session, session.begin():
        bind_owner(session, owner)
        item = Event(
            type="meeting",
            title="Synthetic calendar meeting",
            event_date=date(2026, 11, 10),
            event_time=time(15),
            end_time=None,
            status="pending",
            source="user",
        )
        for key, val in kwargs.items():
            setattr(item, key, val)
        session.add(item)
        session.flush()
        return item.id


def change(value, owner, event_id, **kwargs):
    with value.sessions() as session, session.begin():
        bind_owner(session, owner)
        session.info.update(calendar_capture=True, calendar_timezone="Europe/Moscow")
        item = session.get(Event, event_id)
        for key, val in kwargs.items():
            setattr(item, key, val)


@pytest.mark.parametrize("kind,minutes", [("meeting", 60), ("reminder", 15), ("task", 15)])
def test_timed_defaults(kind, minutes):
    item = SimpleNamespace(
        type=kind,
        title="Synthetic",
        status="pending",
        event_date=date(2026, 11, 1),
        event_time=time(7),
        end_time=None,
    )
    result = event_payload(item, "Europe/Moscow")["payload"]
    assert datetime.fromisoformat(result["end"]["dateTime"]) - datetime.fromisoformat(
        result["start"]["dateTime"]
    ) == timedelta(minutes=minutes)


def test_all_day_overnight_and_missing_date():
    item = SimpleNamespace(
        type="task",
        title="Synthetic",
        status="pending",
        event_date=date(2026, 11, 1),
        event_time=None,
        end_time=None,
    )
    result = event_payload(item, "Europe/Moscow")["payload"]
    assert result["start"] == {"date": "2026-11-01"} and result["end"] == {"date": "2026-11-02"}
    item.event_time, item.end_time = time(23), time(1)
    assert event_payload(item, "Europe/Moscow")["payload"]["end"]["dateTime"].startswith(
        "2026-11-02T01:00"
    )
    item.event_date = None
    with pytest.raises(CalendarError, match="DATE_REQUIRED"):
        event_payload(item, "Europe/Moscow")


def test_cipher_authenticates_owner_and_purpose():
    cipher = TokenCipher(base64.urlsafe_b64encode(b"x" * 32).decode())
    value = cipher.encrypt({"refresh": "synthetic-refresh"}, "owner-a")
    assert "synthetic-refresh" not in value
    assert cipher.decrypt(value, "owner-a")["refresh"] == "synthetic-refresh"
    with pytest.raises(CalendarError, match="TOKEN_DECRYPT_FAILED"):
        cipher.decrypt(value, "owner-b")


def test_oauth_connect_callback_scopes_and_off_by_default(setup):
    a, owner = setup.actor()
    event(setup, owner)
    connect(setup, a)
    status = a.get(PREFIX + "/status").json()
    assert status["connected"] and not status["sync_enabled"]
    assert setup.fake.events == {}
    with setup.service.session(owner) as session:
        row = session.scalar(select(GoogleConnection))
        assert "refresh-" not in row.credentials_cipher and "access-" not in row.credentials_cipher
        assert list(session.scalars(select(GoogleEventLink))) == []


def test_forged_foreign_expired_and_replayed_state(setup):
    a, owner = setup.actor()
    b, _ = setup.actor()
    response = a.post(PREFIX + "/connect", headers=headers(a))
    state = parse_qs(httpx.URL(response.json()["authorization_url"]).query.decode())["state"][0]
    for client, supplied in ((a, "forged"), (b, state)):
        result = client.get(
            PREFIX + "/callback",
            params={"state": supplied, "code": "synthetic"},
            follow_redirects=False,
        )
        assert result.headers["location"].endswith("OAUTH_STATE_INVALID")
    assert setup.fake.calls == []
    good = a.get(
        PREFIX + "/callback", params={"state": state, "code": "synthetic"}, follow_redirects=False
    )
    assert good.headers["location"].endswith("calendar=connected")
    count = len(setup.fake.calls)
    assert (
        a.get(
            PREFIX + "/callback",
            params={"state": state, "code": "synthetic"},
            follow_redirects=False,
        )
        .headers["location"]
        .endswith("OAUTH_STATE_INVALID")
    )
    assert len(setup.fake.calls) == count
    # A second actor's expired state is rejected without exchanging its code.
    response = b.post(PREFIX + "/connect", headers=headers(b))
    expired = parse_qs(httpx.URL(response.json()["authorization_url"]).query.decode())["state"][0]
    with setup.sessions() as session, session.begin():
        other_owner = UUID(b.get("/api/v1/auth/me").json()["id"])
        bind_owner(session, other_owner)
        session.scalar(select(GoogleOAuthState)).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
    assert (
        b.get(
            PREFIX + "/callback",
            params={"state": expired, "code": "synthetic"},
            follow_redirects=False,
        )
        .headers["location"]
        .endswith("OAUTH_STATE_INVALID")
    )


def test_create_update_cancel_delete_and_idempotence(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    item = event(setup, owner)
    for _ in range(2):
        assert (
            a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a)).json()["status"] == "synced"
        )
    assert len(setup.fake.events) == 1
    assert len([c for c in setup.fake.calls if c[0] == "POST" and c[1].endswith("/events")]) == 1
    change(setup, owner, item, title="Synthetic updated meeting")
    setup.service.drain(owner)
    assert next(iter(setup.fake.events.values()))["summary"] == "Synthetic updated meeting"
    change(setup, owner, item, status="cancelled")
    setup.service.drain(owner)
    assert not setup.fake.events
    other = event(setup, owner)
    a.post(f"{PREFIX}/events/{other}/sync", headers=headers(a))
    change(setup, owner, other, status="deleted")
    setup.service.drain(owner)
    assert not setup.fake.events
    with setup.service.session(owner) as session:
        assert (
            session.get(Event, item).status == "cancelled"
            and session.get(Event, other).status == "deleted"
        )


def test_two_owners_connection_and_event_isolation(setup):
    a, owner_a = setup.actor()
    b, owner_b = setup.actor()
    connect(setup, a, "identity-a")
    connect(setup, b, "identity-b")
    enable(a)
    enable(b)
    item = event(setup, owner_a)
    assert b.get(f"{PREFIX}/events/{item}").status_code == 404
    assert b.post(f"{PREFIX}/events/{item}/sync", headers=headers(b)).status_code == 404
    assert (
        a.get(PREFIX + "/status").json()["account_email"]
        != b.get(PREFIX + "/status").json()["account_email"]
    )
    with setup.service.session(owner_a) as session:
        connection_id = session.scalar(select(GoogleConnection.id))
    with setup.service.session(owner_b) as session:
        assert session.get(GoogleConnection, connection_id) is None
    assert not setup.fake.events


def test_network_failure_unknown_insert_outcome_retry_no_duplicate(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    item = event(setup, owner)
    setup.fake.timeout_after_insert = True
    failed = a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a))
    assert failed.status_code == 200 and failed.json()["status"] == "error"
    assert len(setup.fake.events) == 1
    retried = a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a))
    assert retried.json()["status"] == "synced" and len(setup.fake.events) == 1
    with setup.service.session(owner) as session:
        assert session.get(Event, item) is not None


def test_refresh_and_outage_recovery(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    item = event(setup, owner)
    with setup.service.session(owner) as session, session.begin():
        connection = session.scalar(select(GoogleConnection))
        context = setup.service.context(connection)
        tokens = setup.service.cipher.decrypt(connection.credentials_cipher, context)
        tokens["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        connection.credentials_cipher = setup.service.cipher.encrypt(tokens, context)
    setup.fake.fail = True
    assert a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a)).json()["status"] == "error"
    assert setup.fake.refreshes == 1
    setup.fake.fail = False
    assert a.post(PREFIX + "/retry", headers=headers(a)).json()["error_count"] == 0
    assert a.get(f"{PREFIX}/events/{item}").json()["status"] == "synced"


def test_disconnect_preserves_events_reconnect_does_not_duplicate(setup):
    a, owner = setup.actor()
    connect(setup, a, "stable-subject")
    enable(a)
    item = event(setup, owner)
    a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a))
    assert a.post(PREFIX + "/disconnect", headers=headers(a)).json()["remote_events_preserved"]
    assert len(setup.fake.events) == 1
    with setup.service.session(owner) as session:
        assert session.scalar(select(GoogleConnection.credentials_cipher)) is None
        assert session.get(Event, item) is not None
    connect(setup, a, "stable-subject")
    enable(a)
    assert a.get(f"{PREFIX}/events/{item}").json()["status"] == "not_linked"
    a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a))
    assert len(setup.fake.events) == 1


def test_recurrence_is_explicit_single_occurrence_and_new_event_capture(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    old = event(setup, owner)
    change(setup, owner, old, title="Old unlinked updated")
    assert a.get(f"{PREFIX}/events/{old}").json()["status"] == "not_linked"
    item = event(setup, owner, recurrence_rule="weekly", recurrence_series_id=uuid4())
    result = a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a)).json()
    assert result["status"] == "synced" and result["recurrence_note"]
    assert all("recurrence" not in value for value in setup.fake.events.values())
    message = "Создай задачу Synthetic new task завтра"
    setup.nlu.commands[message] = {
        "intent": "CREATE_EVENT",
        "event_type": "TASK",
        "title": "Synthetic new task",
        "date_expression": "завтра",
    }
    created = a.post("/api/v1/assistant/messages", headers=headers(a), json={"message": message})
    assert created.status_code == 200 and created.json()["events"], created.json()
    event_id = created.json()["events"][0]["id"]
    assert a.get(f"{PREFIX}/events/{event_id}").json()["status"] == "synced"


def test_local_update_during_external_call_keeps_new_revision_pending(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    item = event(setup, owner)
    with setup.service.session(owner) as session, session.begin():
        connection = session.scalar(select(GoogleConnection))
        enqueue(session, connection, session.get(Event, item), "Europe/Moscow", explicit=True)

    def concurrent_edit():
        change(setup, owner, item, title="Concurrent latest title")

    setup.fake.on_call = concurrent_edit
    setup.service.drain(owner, limit=1)
    assert a.get(f"{PREFIX}/events/{item}").json()["status"] == "pending"
    setup.service.drain(owner, limit=1)
    assert a.get(f"{PREFIX}/events/{item}").json()["status"] == "synced"
    assert next(iter(setup.fake.events.values()))["summary"] == "Concurrent latest title"


def test_callback_query_is_redacted_before_access_logging():
    import asyncio

    captured = {}

    async def application(scope, receive, send):
        captured.update(scope)

    middleware = api.RedactOAuthQuery(application)
    scope = {
        "type": "http",
        "path": api.CALLBACK,
        "query_string": b"code=synthetic-private-code&state=synthetic-state",
    }
    asyncio.run(middleware(scope, None, None))
    assert scope["query_string"] == b""
    assert captured["state"]["google_oauth_params"]["code"] == "synthetic-private-code"


def test_revoked_refresh_disables_sync_and_preserves_local_event(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    item = event(setup, owner)
    with setup.service.session(owner) as session, session.begin():
        connection = session.scalar(select(GoogleConnection))
        context = setup.service.context(connection)
        tokens = setup.service.cipher.decrypt(connection.credentials_cipher, context)
        tokens["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        connection.credentials_cipher = setup.service.cipher.encrypt(tokens, context)
    setup.fake.revoked = True
    response = a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a))
    assert (
        response.json()["status"] == "error" and response.json()["error_code"] == "REAUTH_REQUIRED"
    )
    status = a.get(PREFIX + "/status").json()
    assert not status["sync_enabled"] and status["error_code"] == "REAUTH_REQUIRED"
    with setup.service.session(owner) as session:
        assert session.get(Event, item).status == "pending"


def test_rollback_does_not_export_and_external_calls_hold_no_transaction(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with setup.service.session(owner) as session, session.begin():
            session.info.update(calendar_capture=True, calendar_timezone="Europe/Moscow")
            session.add(
                Event(
                    type="task",
                    title="Synthetic rolled back",
                    event_date=date(2026, 11, 2),
                    source="user",
                    status="pending",
                )
            )
            session.flush()
            raise RuntimeError("synthetic rollback")
    setup.service.drain(owner)
    assert not setup.fake.events
    item = event(setup, owner)

    def no_transaction_during_network():
        with setup.engine.connect() as c, c.begin():
            c.exec_driver_sql("SET TRANSACTION READ ONLY")
            assert (
                c.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state='idle in transaction'"
                    )
                )
                == 0
            )

    setup.fake.on_call = no_transaction_during_network
    assert a.post(f"{PREFIX}/events/{item}/sync", headers=headers(a)).json()["status"] == "synced"


def test_logout_invalidates_pending_oauth_state(setup):
    a, _ = setup.actor()
    response = a.post(PREFIX + "/connect", headers=headers(a))
    state = parse_qs(httpx.URL(response.json()["authorization_url"]).query.decode())["state"][0]
    assert a.post("/api/v1/auth/logout", headers=headers(a)).status_code == 204
    assert (
        a.get(
            PREFIX + "/callback",
            params={"state": state, "code": "synthetic"},
            follow_redirects=False,
        ).status_code
        == 401
    )
    assert not setup.fake.calls


def test_oauth_cancel_consumes_state_without_exchanging_code(setup):
    a, owner = setup.actor()
    response = a.post(PREFIX + "/connect", headers=headers(a))
    state = parse_qs(httpx.URL(response.json()["authorization_url"]).query.decode())["state"][0]
    cancelled = a.get(
        PREFIX + "/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert cancelled.headers["location"].endswith("OAUTH_CANCELLED")
    with setup.service.session(owner) as session:
        assert list(session.scalars(select(GoogleOAuthState))) == []
    assert not setup.fake.calls


def test_local_config_preserves_other_settings_and_existing_key(tmp_path):
    from dotenv import dotenv_values
    from scripts.configure_google_calendar import REDIRECT, configure

    downloaded = tmp_path / "synthetic-client.json"
    downloaded.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "synthetic-client",
                    "client_secret": "synthetic-secret",
                    "redirect_uris": [REDIRECT],
                }
            }
        ),
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    key = base64.urlsafe_b64encode(b"y" * 32).decode()
    env.write_text(
        "DATABASE_URL=synthetic-preserved\nOPENAI_API_KEY=synthetic-preserved\nGOOGLE_TOKEN_KEY="
        + key
        + "\n",
        encoding="utf-8",
    )
    before = env.read_bytes()
    assert configure(downloaded, env, write=False) is False and env.read_bytes() == before
    assert configure(downloaded, env, write=True) is True
    result = dotenv_values(env)
    assert (
        result["DATABASE_URL"] == "synthetic-preserved"
        and result["OPENAI_API_KEY"] == "synthetic-preserved"
    )
    assert result["GOOGLE_TOKEN_KEY"] == key and result["GOOGLE_REDIRECT_URI"] == REDIRECT


def test_paused_linked_changes_resume_without_exporting_old_unlinked_events(setup):
    a, owner = setup.actor()
    connect(setup, a)
    enable(a)
    selected = event(setup, owner)
    old = event(setup, owner, title="Unselected old event")
    a.post(f"{PREFIX}/events/{selected}/sync", headers=headers(a))
    assert a.post(PREFIX + "/sync", headers=headers(a), json={"enabled": False}).status_code == 200
    change(setup, owner, selected, title="Changed while paused")
    change(setup, owner, old, title="Unselected changed while paused")
    assert a.get(f"{PREFIX}/events/{selected}").json()["status"] == "pending"
    assert next(iter(setup.fake.events.values()))["summary"] != "Changed while paused"
    enable(a)
    assert next(iter(setup.fake.events.values()))["summary"] == "Changed while paused"
    assert a.get(f"{PREFIX}/events/{old}").json()["status"] == "not_linked"


def test_logout_during_code_exchange_cannot_attach_connection(setup):
    from sqlalchemy import delete

    from app.models.accounts import AuthSession

    a, owner = setup.actor()
    response = a.post(PREFIX + "/connect", headers=headers(a))
    state = parse_qs(httpx.URL(response.json()["authorization_url"]).query.decode())["state"][0]
    hashed = authentication.digest(a.cookies["quicklist_session"])

    def logout_while_google_is_in_flight():
        with setup.sessions() as session, session.begin():
            session.execute(delete(AuthSession).where(AuthSession.token_hash == hashed))

    setup.fake.on_call = logout_while_google_is_in_flight
    result = a.get(
        PREFIX + "/callback", params={"state": state, "code": "synthetic"}, follow_redirects=False
    )
    assert result.headers["location"].endswith("OAUTH_STATE_INVALID")
    with setup.service.session(owner) as session:
        assert session.scalar(select(GoogleConnection.credentials_cipher)) is None
    assert any(path.endswith("/revoke") for _, path in setup.fake.calls)
