"""Real TCP HTTP + real PostgreSQL, production DB dependency, no paid provider."""
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from datetime import time as clock
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.ai.openai_provider import get_provider
from app.main import app
from app.models.entities import Conversation, Event
from app.temporal.resolver import TemporalResolver
from tests.fakes import FakeLLMProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def http_api(db, integration_engine, monkeypatch):
    import app.db.session as sessions
    # Legacy TestClient helpers can leave a global DB dependency override behind.
    # These tests must exercise the production dependency in every collection order.
    monkeypatch.delitem(app.dependency_overrides, sessions.get_session, raising=False)
    # Real session factory/bind, not a replacement yield dependency or mock commit.
    monkeypatch.setattr(sessions, 'SessionLocal', sessions.sessionmaker(
        integration_engine, expire_on_commit=False))
    provider = FakeLLMProvider({})
    app.dependency_overrides[get_provider] = lambda: provider
    monkeypatch.setattr(TemporalResolver, 'local_now', lambda self, now=None:
                        now or datetime.fromisoformat('2026-10-20T12:00:00+03:00'))
    observations = []

    async def observed(scope, receive, send):
        async def check_send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                try:
                    body = json.loads(message['body'])
                except json.JSONDecodeError:
                    return await send(message)
                if body.get('conversation_id'):
                    with Session(integration_engine) as check:
                        conversation = check.get(Conversation, UUID(body['conversation_id']))
                        events = [check.get(Event, UUID(item['id'])) for item in body['events']]
                        observations.append({
                            'conversation_visible': conversation is not None,
                            'pending': conversation.pending_action if conversation else None,
                            'events_match': all(saved and saved.status == returned['status']
                                                and saved.event_date.isoformat() == returned['event_date']
                                                for saved, returned in zip(events, body['events'])),
                        })
            await send(message)
        await app(scope, receive, check_send)

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(observed, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(.01)  # Server readiness only, never request/commit retries.
    assert server.started
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=10) as client:
            yield client, provider, observations
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        app.dependency_overrides.pop(get_provider, None)
        assert not thread.is_alive()


def post(client, message, conversation=None):
    return client.post('/api/v1/assistant/messages', json={
        'message': message, 'conversation_id': conversation})


def create_task(client, provider, title='TX отчёт'):
    message = 'Создай задачу ' + title + ' 01.02.2028'
    provider.commands[message] = dict(intent='CREATE_EVENT', event_type='TASK', title=title,
                                      date_expression='01.02.2028')
    response = post(client, message)
    assert response.status_code == 200, response.text
    return response.json()


def assert_visible(observations):
    assert observations and all(row['conversation_visible'] and row['events_match']
                                for row in observations)


def test_create_committed_before_response_body(http_api):
    client, provider, observations = http_api
    create_task(client, provider)
    assert_visible(observations)


def test_update_committed_before_response_body(http_api, integration_engine):
    client, provider, observations = http_api
    with Session(integration_engine) as s, s.begin():
        target = Event(type='task', title='TX отчёт', event_date=date(2028, 2, 1), event_time=clock(9))
        s.add(target)
    provider.commands['Перенеси TX отчёт на 02.02.2028 в 10:00'] = dict(
        intent='UPDATE_EVENT', search_query='TX отчёт', date_expression='02.02.2028', time_expression='в 10:00')
    response = post(client, 'Перенеси TX отчёт на 02.02.2028 в 10:00')
    assert response.status_code == 200, response.text
    assert response.json()['events'], response.text
    assert response.json()['events'][0]['event_date'] == '2028-02-02'
    assert_visible(observations)


def test_pending_and_delete_committed_before_response(http_api, integration_engine):
    client, provider, observations = http_api
    with Session(integration_engine) as s, s.begin():
        s.add(Event(type='task', title='TX отчёт', event_date=date(2028, 2, 1)))
    provider.commands['Удали TX отчёт'] = dict(intent='DELETE_EVENT', search_query='TX отчёт')
    question = post(client, 'Удали TX отчёт').json()
    assert question['missing_fields'] == ['confirmation']
    assert observations[-1]['pending'] is not None
    response = post(client, 'да', question['conversation_id'])
    assert response.status_code == 200 and response.json()['events'][0]['status'] == 'deleted'
    assert observations[-1]['pending'] is None
    assert_visible(observations)


def test_series_confirmation_fast_followup_and_repeat(http_api, integration_engine):
    client, provider, observations = http_api
    sid = uuid4()
    with Session(integration_engine) as s, s.begin():
        events = [Event(type='reminder', title='TX серия', event_date=date(2028, 2, day),
                        event_time=clock(8), recurrence_series_id=sid,
                        recurrence_rule='FREQ=DAILY;COUNT=3') for day in (1, 2, 3)]
        other = Event(type='task', title='TX посторонняя', event_date=date(2028, 2, 1))
        s.add_all([*events, other])
        s.flush()
        ids = {str(e.id) for e in events}
        target = str(events[0].id)
        other_id = other.id
    scope = client.post(f'/api/v1/events/{target}/actions', json={'action': 'DELETE_EVENT'}).json()
    assert scope['missing_fields'] == ['recurrence_scope']
    assert post(client, 'только это событие', scope['conversation_id']).json()['requires_input']
    assert post(client, 'да', scope['conversation_id']).json()['events'][0]['id'] == target
    provider.commands['Удали TX серия всю серию'] = dict(intent='DELETE_EVENT',
        search_query='TX серия', recurrence_scope='series')
    question = post(client, 'Удали TX серия всю серию').json()
    assert question['missing_fields'] == ['confirmation']
    response = post(client, 'да', question['conversation_id'])
    assert response.status_code == 200, response.text
    assert {e['id'] for e in response.json()['events']} == ids - {target}
    repeat = post(client, 'да', question['conversation_id'])
    assert repeat.status_code == 200 and repeat.json()['events'] == []
    with Session(integration_engine) as s:
        saved = s.scalars(select(Event).where(Event.recurrence_series_id == sid)).all()
        assert len(saved) == 3 and all(e.status == 'deleted' and e.recurrence_rule for e in saved)
        assert s.get(Event, other_id).status == 'pending'
    assert_visible(observations)


def test_provider_runs_without_database_transaction(http_api, integration_engine):
    client, provider, _ = http_api
    parse = provider.parse_command
    def checked_parse(message, context):
        with integration_engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE "
                "datname=current_database() AND pid<>pg_backend_pid() AND xact_start IS NOT NULL")) == 0
        return parse(message, context)
    provider.parse_command = checked_parse
    create_task(client, provider)


def test_commit_failure_is_503_and_rolls_back_entire_batch(http_api, integration_engine):
    client, provider, _ = http_api
    # Real deferred PostgreSQL trigger: failure is raised at COMMIT, not mocked.
    with integration_engine.begin() as c:
        c.exec_driver_sql("CREATE FUNCTION tx_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                          "BEGIN RAISE EXCEPTION 'isolated deferred failure'; RETURN NEW; END $$")
        c.exec_driver_sql('CREATE CONSTRAINT TRIGGER tx_fail AFTER INSERT ON events '
                          'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tx_fail_commit()')
    provider.commands['Напомни 01.02.2028 в 09:00 TX альфа и TX бета'] = dict(
        intent='CREATE_EVENT', actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title=title,
            date_expression='01.02.2028',time_expression='в 09:00') for title in ['TX альфа','TX бета']])
    try:
        question = post(client, 'Напомни 01.02.2028 в 09:00 TX альфа и TX бета').json()
        response = post(client, 'да', question['conversation_id'])
        assert response.status_code == 503, response.text
        with Session(integration_engine) as check:
            assert check.scalars(select(Event)).all() == []
            assert check.get(Conversation, UUID(question['conversation_id'])).pending_action is not None
    finally:
        with integration_engine.begin() as c:
            c.exec_driver_sql('DROP TRIGGER tx_fail ON events')
            c.exec_driver_sql('DROP FUNCTION tx_fail_commit()')


def test_concurrent_confirmation_applies_once(http_api, integration_engine):
    client, provider, _ = http_api
    provider.commands['Напомни 01.02.2028 в 09:00 TX альфа и TX бета'] = dict(
        intent='CREATE_EVENT', actions=[dict(intent='CREATE_EVENT',event_type='REMINDER',title=title,
            date_expression='01.02.2028',time_expression='в 09:00') for title in ['TX альфа','TX бета']])
    question = post(client, 'Напомни 01.02.2028 в 09:00 TX альфа и TX бета').json()
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: post(client, 'да', question['conversation_id']), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert sorted(len(r.json()['events']) for r in responses) == [0, 2]
    with Session(integration_engine) as check:
        assert len(check.scalars(select(Event)).all()) == 2
        assert check.get(Conversation, UUID(question['conversation_id'])).pending_action is None


def test_genuine_missing_conversation_still_404(http_api):
    client, _, _ = http_api
    response = post(client, 'да', str(uuid4()))
    assert response.status_code == 404
    assert response.json() == {'detail': 'Диалог не найден. Начните новый диалог.'}


def test_changed_pending_during_provider_call_cannot_consume_old_yes(http_api, integration_engine):
    client, provider, _ = http_api
    target = create_task(client, provider)
    cid, eid = target['conversation_id'], target['events'][0]['id']
    entered, release = threading.Event(), threading.Event()
    original = provider.parse_command

    def paused(message, context):
        if message == 'да':
            entered.set()
            assert release.wait(5)
        return original(message, context)

    provider.parse_command = paused
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(post, client, 'да', cid)
        assert entered.wait(5)
        try:
            question = client.post(f'/api/v1/events/{eid}/actions', json={
                'action': 'DELETE_EVENT', 'conversation_id': cid})
            assert question.status_code == 200 and question.json()['requires_input']
        finally:
            release.set()
        rejected = future.result()
    assert rejected.status_code == 503
    with Session(integration_engine) as check:
        assert check.get(Event, UUID(eid)).status == 'pending'
        assert check.get(Conversation, UUID(cid)).pending_action['confirmation_kind'] == 'delete'


@pytest.mark.parametrize('operation', ['create', 'update', 'delete'])
def test_real_deferred_commit_failure_never_reports_success(http_api, integration_engine, operation):
    client, provider, _ = http_api
    target = create_task(client, provider)
    eid, cid = target['events'][0]['id'], target['conversation_id']
    if operation == 'delete':
        question = client.post(f'/api/v1/events/{eid}/actions', json={
            'action': 'DELETE_EVENT', 'conversation_id': cid})
        assert question.status_code == 200 and question.json()['requires_input']
    with integration_engine.begin() as c:
        c.exec_driver_sql("CREATE FUNCTION tx_reject() RETURNS trigger LANGUAGE plpgsql AS $$ "
                          "BEGIN RAISE EXCEPTION 'isolated commit rejection'; RETURN NEW; END $$")
        c.exec_driver_sql('CREATE CONSTRAINT TRIGGER tx_reject AFTER INSERT OR UPDATE ON events '
                          'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION tx_reject()')
    try:
        if operation == 'create':
            message = 'Создай задачу TX другая 01.02.2028'
            provider.commands[message] = dict(intent='CREATE_EVENT',event_type='TASK',title='TX другая',
                                             date_expression='01.02.2028')
            response = post(client, message, cid)
        elif operation == 'update':
            message = 'Перенеси TX отчёт на 02.02.2028 в 10:00'
            provider.commands[message] = dict(intent='UPDATE_EVENT',search_query='TX отчёт',
                date_expression='02.02.2028',time_expression='в 10:00')
            response = post(client, message, cid)
        else:
            response = post(client, 'да', cid)
        assert response.status_code == 503, response.text
        with Session(integration_engine) as check:
            saved = check.get(Event, UUID(eid))
            assert saved.status == 'pending' and saved.event_date == date(2028, 2, 1)
            assert len(check.scalars(select(Event)).all()) == 1
            if operation == 'delete':
                assert check.get(Conversation, UUID(cid)).pending_action['confirmation_kind'] == 'delete'
    finally:
        with integration_engine.begin() as c:
            c.exec_driver_sql('DROP TRIGGER tx_reject ON events')
            c.exec_driver_sql('DROP FUNCTION tx_reject()')


@pytest.mark.parametrize('answer', ['нет', 'отмена'])
def test_series_cancel_does_not_touch_other_events(http_api, integration_engine, answer):
    client, provider, observations = http_api
    sid = uuid4()
    with Session(integration_engine) as s, s.begin():
        s.add_all([Event(type='reminder',title='TX серия',event_date=date(2028,2,day),
                        event_time=clock(8),recurrence_series_id=sid,
                        recurrence_rule='FREQ=DAILY;COUNT=2') for day in (1,2)])
    provider.commands['Удали TX серия всю серию'] = dict(intent='DELETE_EVENT',
        search_query='TX серия',recurrence_scope='series')
    question = post(client, 'Удали TX серия всю серию').json()
    assert question['missing_fields'] == ['confirmation']
    response = post(client, answer, question['conversation_id'])
    assert response.status_code == 200 and not response.json()['requires_input']
    with Session(integration_engine) as check:
        assert all(e.status == 'pending' for e in check.scalars(select(Event)))
        assert check.get(Conversation, UUID(question['conversation_id'])).pending_action is None
    assert_visible(observations)


def test_stale_target_confirmation_does_not_delete_another_record(http_api, integration_engine):
    client, provider, _ = http_api
    target = create_task(client, provider)
    eid, cid = target['events'][0]['id'], target['conversation_id']
    question = client.post(f'/api/v1/events/{eid}/actions', json={
        'action': 'DELETE_EVENT', 'conversation_id': cid})
    assert question.status_code == 200 and question.json()['requires_input']
    with Session(integration_engine) as s, s.begin():
        s.get(Event, UUID(eid)).status = 'deleted'
        s.add(Event(type='task',title='TX отчёт',event_date=date(2028,2,1)))
    response = post(client, 'да', cid)
    assert response.status_code == 200 and response.json()['events'] == []
    with Session(integration_engine) as check:
        assert len(check.scalars(select(Event).where(Event.status == 'pending')).all()) == 1
        assert check.get(Conversation, UUID(cid)).pending_action is None

