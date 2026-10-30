"""Real PostgreSQL and real session authentication; no LLM in account planner reads."""
import os
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.openai_provider import get_provider
from app.api import auth, routes
from app.core.config import Settings
from app.core.passwords import hash_password
from app.db import session as database
from app.db.ownership import bind_owner
from app.main import app
from app.models.accounts import User
from app.models.entities import Conversation, Event
from app.services import authentication
from app.services.indexing_target import verify_indexing_target

DAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 9, tzinfo=UTC)  # noon in Moscow
ORIGIN = 'https://planner.example.test'
PASSWORD = 'Synthetic-only-planner-password-123'


@pytest.fixture
def planner(monkeypatch):
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Verified disposable PostgreSQL required')
    engine = create_engine(url, hide_parameters=True, connect_args={'connect_timeout': 3})
    assert engine.url.database == 'stage82_daily_planner_20261028_test'
    verify_indexing_target(engine, '7698854534603636773')
    with engine.begin() as conn:
        assert conn.scalar(text('SELECT version_num FROM alembic_version')) == '0009'
        conn.exec_driver_sql('TRUNCATE account_tokens,auth_sessions,auth_throttles,google_oauth_states,google_event_links,google_connections,conversations,event_search_documents,events,embedding_cache')
        conn.exec_driver_sql('DELETE FROM users WHERE NOT is_legacy_owner')
    settings = Settings(_env_file=None, database_url=url, auth_origin=ORIGIN, auth_cookie_secure=True,
                        auth_require_email_verification=True, result_limit=3)
    for module in (authentication, auth, routes):
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
    monkeypatch.setattr(database, 'SessionLocal', sessionmaker(engine, expire_on_commit=False))
    a, b = uuid4(), uuid4()
    with Session(engine, info={'system_access': True}) as session, session.begin():
        encoded = hash_password(PASSWORD)
        session.add_all([User(id=key,email=email,password_hash=encoded,email_verified_at=NOW)
                         for key,email in [(a,'planner-a@example.test'),(b,'planner-b@example.test')]])
    previous = dict(app.dependency_overrides)
    calls = []
    def no_llm():
        calls.append(True)
        raise AssertionError('Daily plan must not instantiate an LLM')
    app.dependency_overrides[get_provider] = no_llm
    if hasattr(routes, 'get_plan_now'):
        app.dependency_overrides[routes.get_plan_now] = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, engine, a, b, calls
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()


def login(client, email='planner-a@example.test'):
    token = client.get('/api/v1/auth/csrf').json()['csrf_token']
    response = client.post('/api/v1/auth/login',json={'email':email,'password':PASSWORD},
                           headers={'Origin':ORIGIN,'X-CSRF-Token':token})
    assert response.status_code == 200


def event(engine, owner, title, day=DAY, clock=None, **extra):
    key = uuid4()
    with Session(engine) as session, session.begin():
        bind_owner(session, owner)
        session.add(Event(id=key,type='task',title=title,event_date=day,event_time=clock,**extra))
    return str(key)


def get(client):
    response = client.get('/api/v1/events/today')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_auth_required_and_unverified_account_denied(planner):
    client, engine, a, _, _ = planner
    assert client.get('/api/v1/events/today').status_code == 401
    login(client)
    with Session(engine, info={'system_access':True}) as session, session.begin():
        session.get(User,a).email_verified_at = None
    assert client.get('/api/v1/events/today').status_code == 403


def test_account_scope_ignores_active_conversation_and_foreign_owner_parameter(planner):
    client, engine, a, b, calls = planner
    own = event(engine,a,'From another dialogue',clock=time(15))
    foreign = event(engine,b,'Foreign account secret',clock=time(13))
    with Session(engine) as session, session.begin():
        bind_owner(session,a)
        session.add_all([Conversation(),Conversation()])
    login(client)
    plan = get(client)
    assert [e['id'] for e in plan['upcoming']] == [own]
    assert foreign not in str(plan) and 'Foreign account secret' not in str(plan)
    spoof = client.get('/api/v1/events/today?owner_id='+str(b)).json()
    assert spoof == plan
    assert calls == []
    login(client,'planner-b@example.test')
    assert [e['id'] for e in get(client)['upcoming']] == [foreign]


def test_groups_counts_and_closest_time_sort_are_explicit(planner):
    client, engine, a, _, _ = planner
    late = event(engine,a,'Later',clock=time(18))
    first = event(engine,a,'First',clock=time(12))
    middle = event(engine,a,'Middle',clock=time(15))
    untimed = event(engine,a,'Without time')
    elapsed = event(engine,a,'Elapsed today',clock=time(9))
    earlier = event(engine,a,'Earlier unfinished',day=DAY-timedelta(days=1))
    event(engine,a,'Undated',day=None)
    event(engine,a,'Tomorrow',day=DAY+timedelta(days=1),clock=time(13))
    for status in ('completed','cancelled','deleted'):
        event(engine,a,status,clock=time(14),status=status)
    login(client)
    plan = get(client)
    assert plan['today'] == '2026-10-28' and plan['timezone'] == 'Europe/Moscow'
    assert plan['total_today'] == 5
    assert [e['id'] for e in plan['upcoming']] == [first,middle,late]
    assert [e['id'] for e in plan['without_time']] == [untimed]
    assert [e['id'] for e in plan['overdue_today']] == [elapsed]
    assert [e['id'] for e in plan['overdue_earlier']] == [earlier]
    assert plan['undated_count'] == 1


def test_real_stored_recurrence_occurrences_do_not_expand_or_collapse(planner):
    client, engine, a, _, _ = planner
    series = uuid4()
    today = event(engine,a,'Recurring today',clock=time(16),recurrence_series_id=series,recurrence_rule='FREQ=DAILY')
    event(engine,a,'Recurring tomorrow',day=DAY+timedelta(days=1),clock=time(16),recurrence_series_id=series,recurrence_rule='FREQ=DAILY')
    moved = event(engine,a,'Moved occurrence',clock=time(17),recurrence_series_id=series,recurrence_rule='FREQ=DAILY')
    login(client)
    plan = get(client)
    assert [e['id'] for e in plan['upcoming']] == [today,moved]
    assert all(e['recurrence_series_id'] == str(series) for e in plan['upcoming'])


def test_timezone_midnight_and_overnight_in_progress(planner):
    client, engine, a, _, _ = planner
    app.dependency_overrides[routes.get_plan_now] = lambda: datetime(2026,10,28,21,30,tzinfo=UTC)
    ongoing = event(engine,a,'Overnight',clock=time(23),end_time=time(1))
    later = event(engine,a,'After midnight',day=DAY+timedelta(days=1),clock=time(0,45))
    event(engine,a,'Yesterday timed',clock=time(22))
    login(client)
    plan = get(client)
    assert plan['today'] == '2026-10-29'
    assert [e['id'] for e in plan['upcoming']] == [ongoing,later]
    assert plan['ongoing_ids'] == [ongoing]
    assert len(plan['overdue_earlier']) == 1
    assert plan['total_today'] == 2


def test_ended_interval_is_overdue_but_current_interval_is_not(planner):
    client, engine, a, _, _ = planner
    ended = event(engine,a,'Ended',clock=time(10),end_time=time(12))
    ongoing = event(engine,a,'In progress',clock=time(11),end_time=time(13))
    login(client)
    plan = get(client)
    assert [e['id'] for e in plan['overdue_today']] == [ended]
    assert [e['id'] for e in plan['upcoming']] == [ongoing]
    assert plan['ongoing_ids'] == [ongoing]


def test_all_today_is_not_chat_result_limit_or_five_preview_items(planner):
    client, engine, a, _, _ = planner
    for index in range(12):
        event(engine,a,'Item '+str(index),clock=time(12,index))
    login(client)
    plan = get(client)
    assert len(plan['upcoming']) == plan['total_today'] == 12


def test_new_committed_schedule_and_status_are_visible_without_dialogue(planner):
    client, engine, a, _, _ = planner
    login(client)
    assert get(client)['total_today'] == 0
    key = event(engine,a,'Changes',clock=time(16))
    assert get(client)['total_today'] == 1
    for changes, expected in [({'event_date':DAY+timedelta(days=1)},0),
                              ({'event_date':DAY},1),({'status':'completed'},0),({'status':'deleted'},0)]:
        with Session(engine) as session, session.begin():
            bind_owner(session,a)
            row = session.get(Event,__import__('uuid').UUID(key))
            for field,value in changes.items():
                setattr(row,field,value)
        assert get(client)['total_today'] == expected


def test_get_is_read_only_and_does_not_create_conversations(planner):
    client, engine, a, _, _ = planner
    event(engine,a,'Read only',clock=time(16))
    login(client)
    def snapshot():
        with engine.connect() as conn,conn.begin():
            conn.exec_driver_sql('SET TRANSACTION READ ONLY')
            return [conn.scalar(text("SELECT md5(coalesce(string_agg(to_jsonb(t)::text,'' ORDER BY to_jsonb(t)::text),'')) FROM "+table+' t')) for table in ('events','conversations','auth_sessions')]
    before = snapshot()
    get(client)
    get(client)
    assert snapshot() == before


def test_database_failure_returns_safe_503(planner, monkeypatch):
    client, _, _, _, _ = planner
    login(client)
    from app.repositories.events import EventRepository
    def unavailable(*args):
        raise SQLAlchemyError('Private database diagnostic')
    monkeypatch.setattr(EventRepository,'daily_candidates',unavailable)
    response = client.get('/api/v1/events/today')
    assert response.status_code == 503
    assert 'Private database diagnostic' not in response.text


def test_planner_rejects_a_naive_clock():
    from app.services.today_plan import build_today_plan
    with pytest.raises(ValueError,match='timezone-aware'):
        build_today_plan(None,'Europe/Moscow',datetime(2026,10,28,12))
