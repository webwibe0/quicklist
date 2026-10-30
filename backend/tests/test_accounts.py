"""Targeted real-PostgreSQL account and tenant leakage checks. No paid providers."""

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.embeddings import FakeEmbeddingProvider
from app.ai.openai_provider import get_provider
from app.api import routes
from app.core.config import Settings
from app.core.passwords import verify_password
from app.db import session as database
from app.db.ownership import OwnershipError, bind_owner
from app.main import app
from app.models.accounts import AuthSession, User
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.commands import SearchEventsCommand
from app.services import authentication
from app.services.indexing import IndexingWorker
from app.services.indexing_target import verify_indexing_target
from app.services.message_transaction import execute_message
from tests.fakes import FakeLLMProvider
from tests.test_embedding_provider import unit

ORIGIN = "http://127.0.0.1:3001"
PASSWORD = "Synthetic-only-password-42"
LEGACY = UUID("20000000-0000-4000-8000-000000000001")


@pytest.fixture(scope="module")
def account_engine():
    url = os.environ.get("STAGE10_TEST_DATABASE_URL")
    if not url:
        pytest.skip("STAGE10_TEST_DATABASE_URL is required")
    engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 3})
    assert engine.url.host == "127.0.0.1" and engine.url.port == 55434
    assert engine.url.database == "stage82_stage10_20261026_test"
    verify_indexing_target(engine, "7698854534603636773")
    yield engine
    engine.dispose()


@pytest.fixture
def accounts(account_engine, monkeypatch):
    verify_indexing_target(account_engine, "7698854534603636773")
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(account_engine, expire_on_commit=False)
    )
    settings = Settings(
        _env_file=None,
        database_url=os.environ["STAGE10_TEST_DATABASE_URL"],
        auth_origin=ORIGIN,
        semantic_search_enabled=True,
        embedding_real_api_enabled=False,
        embedding_worker_enabled=False,
        embedding_production_indexing_approved=False,
        auth_login_ip_limit=100,
        embedding_allowed_cluster_id="7698854534603636773",
    )
    monkeypatch.setattr(authentication, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    nlu = FakeLLMProvider({})
    embeddings = FakeEmbeddingProvider()
    original_execute = execute_message
    monkeypatch.setattr(
        routes,
        "execute_message",
        lambda *args: original_execute(*args, embedding_provider=embeddings),
    )
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_provider] = lambda: nlu
    clients = []

    def client():
        value = TestClient(app, client=("synthetic-" + uuid4().hex, 50000))
        value.__enter__()
        clients.append(value)
        return value

    yield client, nlu, embeddings, settings, account_engine
    for value in clients:
        value.__exit__(None, None, None)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(overrides)


def headers(client):
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("quicklist_csrf", "")}


def register(client, email=None):
    assert client.get("/api/v1/auth/csrf").status_code == 200
    email = email or "user-" + uuid4().hex + "@example.test"
    response = client.post(
        "/api/v1/auth/register",
        headers=headers(client),
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response.json(), email


def task(client, nlu, title):
    message = "Создай задачу " + title + " завтра"
    nlu.commands[message] = dict(
        intent="CREATE_EVENT", event_type="TASK", title=title, date_expression="завтра"
    )
    response = client.post(
        "/api/v1/assistant/messages", headers=headers(client), json={"message": message}
    )
    assert response.status_code == 200, response.text
    assert not response.json()["requires_input"], response.text
    return response.json()


def test_registration_session_hashes_and_opaque_cookie(accounts):
    factory, _, _, _, engine = accounts
    client = factory()
    assert client.get("/api/v1/auth/me").status_code == 401
    client.get("/api/v1/auth/csrf")
    old_cookie = client.cookies["quicklist_session"]
    response = client.post(
        "/api/v1/auth/register",
        headers=headers(client),
        json={"email": "UPPER-" + uuid4().hex + "@EXAMPLE.TEST", "password": PASSWORD},
    )
    assert response.status_code == 201
    user = response.json()
    assert user["email"] == user["email"].lower() and not user["is_legacy_owner"]
    assert client.cookies["quicklist_session"] != old_cookie
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert "SameSite=lax" in response.headers.get_list("set-cookie")[0]
    assert client.get("/api/v1/auth/me").json() == user
    with Session(engine) as session:
        stored = session.get(User, UUID(user["id"]))
        assert stored.password_hash.startswith("scrypt$131072$8$1$")
        assert PASSWORD not in stored.password_hash and verify_password(
            PASSWORD, stored.password_hash
        )
        assert session.get(AuthSession, authentication.digest(old_cookie)) is None
        assert session.get(AuthSession, authentication.digest(client.cookies["quicklist_session"]))


def test_login_logout_persistence_and_session_rotation(accounts):
    factory, nlu, _, _, _ = accounts
    client = factory()
    user, email = register(client)
    created = task(client, nlu, "S10 persistence " + uuid4().hex)
    old_cookie = client.cookies["quicklist_session"]
    old_csrf = client.cookies["quicklist_csrf"]
    assert client.post("/api/v1/auth/logout", headers=headers(client)).status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 401
    replay = factory()
    replay.cookies.set("quicklist_session", old_cookie)
    replay.cookies.set("quicklist_csrf", old_csrf)
    assert replay.get("/api/v1/auth/me").status_code == 401
    client.get("/api/v1/auth/csrf")
    login = client.post(
        "/api/v1/auth/login", headers=headers(client), json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200 and login.json()["id"] == user["id"]
    history = client.get("/api/v1/conversations/" + created["conversation_id"])
    assert history.status_code == 200 and len(history.json()["entries"]) == 2
    assert history.json()["entries"][1]["response"]["events"][0]["id"] == created["events"][0]["id"]


def test_csrf_origin_and_mass_assignment_fail_closed(accounts):
    factory, _, _, _, _ = accounts
    client = factory()
    user, _ = register(client)
    assert client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN}).status_code == 403
    forged = headers(client) | {"Origin": "https://attacker.invalid"}
    assert client.post("/api/v1/auth/logout", headers=forged).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout", headers=headers(client) | {"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    response = client.post(
        "/api/v1/assistant/messages",
        headers=headers(client),
        json={"message": "S10", "user_id": str(LEGACY)},
    )
    assert response.status_code == 422
    client.cookies.set("quicklist_session", "forged-cookie")
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_throttles_and_password_not_echoed(accounts):
    factory, _, _, settings, _ = accounts
    client = factory()
    _, email = register(client)
    client.post("/api/v1/auth/logout", headers=headers(client))
    client.get("/api/v1/auth/csrf")
    settings.auth_login_account_limit = 2
    for _ in range(2):
        response = client.post(
            "/api/v1/auth/login",
            headers=headers(client),
            json={"email": email, "password": PASSWORD + "-wrong"},
        )
        assert response.status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login",
            headers=headers(client),
            json={"email": email, "password": PASSWORD},
        ).status_code
        == 429
    )
    bad = client.post(
        "/api/v1/auth/login",
        headers=headers(client),
        json={"email": email, "password": "tiny-secret"},
    )
    assert bad.status_code == 422 and "tiny-secret" not in bad.text


def test_anonymous_protected_and_voice_endpoints_never_reach_provider(accounts):
    factory, nlu, embeddings, _, _ = accounts
    client = factory()
    assert client.get("/api/v1/conversations").status_code == 401
    assert client.post("/api/v1/assistant/messages", json={"message": "S10"}).status_code in (
        401,
        403,
    )
    assert client.post("/api/v1/voice/speech", json={"text": "S10"}).status_code in (401, 403)
    assert client.post("/api/v1/voice/transcriptions", content=b"bad").status_code in (401, 403)
    assert not embeddings.calls


def test_two_users_exact_duplicates_and_foreign_event_uuid(accounts):
    factory, nlu, _, _, engine = accounts
    a, b = factory(), factory()
    user_a, _ = register(a)
    user_b, _ = register(b)
    title = "S10 identical " + uuid4().hex
    own_a, own_b = task(a, nlu, title), task(b, nlu, title)
    assert own_a["events"][0]["id"] != own_b["events"][0]["id"]
    for action in ("DELETE_EVENT", "COMPLETE_EVENT"):
        response = b.post(
            "/api/v1/events/" + own_a["events"][0]["id"] + "/actions",
            headers=headers(b),
            json={"action": action},
        )
        assert response.status_code == 404
    assert b.get("/api/v1/conversations/" + own_a["conversation_id"]).status_code == 404
    response = b.post(
        "/api/v1/assistant/messages",
        headers=headers(b),
        json={"conversation_id": own_a["conversation_id"], "message": "S10"},
    )
    assert response.status_code == 404
    move = "Перенеси " + title + " завтра в 15:00"
    nlu.commands[move] = dict(
        intent="UPDATE_EVENT",
        search_query=title,
        date_expression="завтра",
        time_expression="в 15:00",
    )
    moved = b.post("/api/v1/assistant/messages", headers=headers(b), json={"message": move})
    assert moved.status_code == 200 and moved.json()["events"][0]["id"] == own_b["events"][0]["id"]
    with Session(engine) as session:
        bind_owner(session, UUID(user_a["id"]))
        assert session.get(Event, UUID(own_a["events"][0]["id"])).status == "pending"
        assert session.get(Event, UUID(own_a["events"][0]["id"])).event_time is None
        assert session.get(Event, UUID(own_b["events"][0]["id"])) is None
        assert {str(row.owner_id) for row in session.scalars(select(Conversation))} == {
            user_a["id"]
        }


def test_pending_action_and_messages_are_not_shared(accounts):
    factory, nlu, _, _, engine = accounts
    a, b = factory(), factory()
    ua, _ = register(a)
    ub, _ = register(b)
    message = "Напомни S10 позвонить завтра"
    nlu.commands[message] = dict(
        intent="CREATE_EVENT",
        event_type="REMINDER",
        title="S10 позвонить",
        date_expression="завтра",
    )
    pending = a.post(
        "/api/v1/assistant/messages", headers=headers(a), json={"message": message}
    ).json()
    assert pending["requires_input"] and "time" in pending["missing_fields"]
    response = b.post(
        "/api/v1/assistant/messages",
        headers=headers(b),
        json={"conversation_id": pending["conversation_id"], "message": "В 15:00"},
    )
    assert response.status_code == 404
    assert not b.get("/api/v1/conversations").json()
    with Session(engine) as session:
        bind_owner(session, UUID(ua["id"]))
        conversation = session.get(Conversation, UUID(pending["conversation_id"]))
        assert conversation.pending_action and len(conversation.messages) == 2


def test_owner_scope_raw_sql_series_and_composite_foreign_key(accounts):
    factory, _, _, _, engine = accounts
    a, b = factory(), factory()
    ua, _ = register(a)
    ub, _ = register(b)
    series = uuid4()
    with Session(engine) as session, session.begin():
        bind_owner(session, UUID(ua["id"]))
        first = Event(
            type="task",
            title="S10 series A",
            recurrence_series_id=series,
            recurrence_rule="FREQ=DAILY",
        )
        session.add(first)
        session.flush()
        first_id = first.id
    with Session(engine) as session, session.begin():
        bind_owner(session, UUID(ub["id"]))
        second = Event(
            type="task",
            title="S10 series B",
            recurrence_series_id=series,
            recurrence_rule="FREQ=DAILY",
        )
        session.add(second)
        session.flush()
        second_id = second.id
    with Session(engine) as session:
        with pytest.raises(OwnershipError):
            session.scalars(select(Event)).all()
        bind_owner(session, UUID(ua["id"]))
        assert [
            e.id for e in EventRepository(session, 50).get_series_events(series, "UPDATE_EVENT")
        ] == [first_id]
        assert session.get(Event, second_id) is None
        with pytest.raises(OwnershipError):
            session.execute(text("SELECT * FROM events")).all()
        with pytest.raises(OwnershipError):
            bind_owner(session, UUID(ub["id"]))
        session.rollback()
    with Session(engine) as session:
        bind_owner(session, UUID(ua["id"]))
        with pytest.raises(IntegrityError), session.begin():
            session.add(Conversation(last_event_id=second_id))
            session.flush()


def test_hybrid_sql_and_background_projection_ownership(accounts):
    factory, nlu, embeddings, settings, engine = accounts
    a, b = factory(), factory()
    ua, _ = register(a)
    ub, _ = register(b)
    title = "S10 поиск " + uuid4().hex
    own_a, own_b = task(a, nlu, title), task(b, nlu, title)
    embeddings.vectors[title] = unit()
    worker_settings = settings.model_copy(
        update={"embedding_worker_enabled": True, "embedding_max_documents": 1000}
    )
    worker = IndexingWorker(engine, embeddings, worker_settings)
    after = None
    for _ in range(3):
        stats = worker.run_once(after=after)
        assert stats["failed"] == 0
        after = UUID(stats["next_cursor"]) if stats["next_cursor"] else None
        if after is None:
            break
    message = "Найди " + title
    nlu.commands[message] = dict(intent="SEARCH_EVENTS", search_query=title)
    for client, expected, foreign in ((a, own_a, own_b), (b, own_b, own_a)):
        response = client.post(
            "/api/v1/assistant/messages", headers=headers(client), json={"message": message}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["search_mode"] == "hybrid"
        assert [item["id"] for item in body["events"]] == [expected["events"][0]["id"]]
        assert foreign["events"][0]["id"] not in str(body)
    with Session(engine) as session:
        bind_owner(session, UUID(ua["id"]))
        found = EventRepository(session, 50).search(SearchEventsCommand(query=title))
        assert [e.id for e in found] == [UUID(own_a["events"][0]["id"])]


def test_reserved_owner_not_claimable_and_legacy_history_gate(accounts):
    factory, _, _, _, engine = accounts
    with Session(engine) as session:
        reserved = session.get(User, LEGACY)
        assert reserved is not None and reserved.is_legacy_owner
        reserved_email = reserved.email
    client = factory()
    client.get("/api/v1/auth/csrf")
    response = client.post(
        "/api/v1/auth/register",
        headers=headers(client),
        json={"email": reserved_email, "password": PASSWORD},
    )
    assert response.status_code == 409
    normal = factory()
    user, _ = register(normal)
    assert not user["is_legacy_owner"]
    assert (
        normal.post(
            "/api/v1/conversations/" + str(uuid4()) + "/legacy-history",
            headers=headers(normal),
            json={"entries": []},
        ).status_code
        == 403
    )
    with Session(engine) as session:
        bind_owner(session, LEGACY)
        assert len(list(session.scalars(select(Event)))) == 1177
        assert session.get(User, LEGACY).password_hash is None


def test_expired_session_is_rejected(accounts):
    from datetime import UTC, datetime, timedelta

    factory, _, _, _, engine = accounts
    client = factory()
    register(client)
    token = authentication.digest(client.cookies["quicklist_session"])
    with Session(engine) as session, session.begin():
        session.get(AuthSession, token).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/conversations").status_code == 401


def test_https_cookies_use_host_prefix_and_logout_removes_them(accounts):
    _, _, _, settings, _ = accounts
    settings.auth_origin = "https://secure.example.test"
    settings.auth_cookie_secure = True
    with TestClient(
        app, base_url=settings.auth_origin, client=("secure-cookie-test", 50000)
    ) as client:
        assert client.get("/api/v1/auth/csrf").status_code == 200
        csrf = client.cookies["__Host-quicklist_csrf"]
        response = client.post(
            "/api/v1/auth/register",
            headers={"Origin": settings.auth_origin, "X-CSRF-Token": csrf},
            json={"email": "secure-" + uuid4().hex + "@example.test", "password": PASSWORD},
        )
        assert response.status_code == 201
        for value in response.headers.get_list("set-cookie"):
            assert value.startswith("__Host-") and "Secure" in value and "Domain=" not in value
        assert client.get("/api/v1/auth/me").status_code == 200
        response = client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": settings.auth_origin,
                "X-CSRF-Token": client.cookies["__Host-quicklist_csrf"],
            },
        )
        assert response.status_code == 204
        assert not client.cookies and client.get("/api/v1/auth/me").status_code == 401
