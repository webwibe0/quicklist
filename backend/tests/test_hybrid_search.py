"""Bounded synthetic smoke; scripted vectors test integration, NOT language quality."""
from contextlib import nullcontext
from datetime import date, time
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ai.embeddings import EmbeddingError, EmbeddingProfile, FakeEmbeddingProvider
from app.ai.openai_provider import get_provider
from app.api import routes
from app.core.config import Settings
from app.db.session import get_session
from app.main import app
from app.models.entities import Conversation, Event
from app.repositories.events import EventRepository
from app.schemas.commands import ParsedCommand, SearchEventsCommand
from app.services.hybrid_search import HybridSearch
from app.services.indexing_target import IndexingTargetError, verify_worker_target
from app.services.message_transaction import PreparedProvider, execute_message
from app.services.search_filters import literal_query
from app.services.search_guard import constraint_assessment
from tests.fakes import FakeLLMProvider
from tests.test_embedding_provider import unit


def config(**kwargs):
    return Settings(_env_file=None, **({"semantic_search_enabled": True} | kwargs))


def event(title, **kwargs):
    return Event(id=uuid4(), title=title, type='task', status='pending', source='user',
                 type_inferred=False, **kwargs)


def search(lexical, semantic, query, *, error=None, enabled=True):
    repo = Mock(limit=50)
    repo.search.return_value = lexical
    repo.semantic.return_value = semantic
    if error:
        repo.semantic.side_effect = error
    repo.session.begin_nested.return_value = nullcontext()
    prepared = PreparedProvider()
    prepared.embedding_test_provider = True
    prepared.embedding_request, prepared.embedding_vector = query, unit()
    service = HybridSearch(repo, config(semantic_search_enabled=enabled), prepared)
    return service.execute(SearchEventsCommand(query=query)), service, repo


@pytest.mark.parametrize('query,title,state', [
    ('Позвонить Светлане', 'Позвонить Светлане', 'PASS'),
    ('Связаться со Светланой', 'Позвонить Светлане', 'PASS'),
    ('Связаться со Светланой', 'Позвонить Марине', 'CONTRADICTION'),
    ('связаться со светланой', 'Позвонить Марине', 'UNVERIFIED'),
    ('Связаться со Светланой, не со Мариной', 'Позвонить Светлане и Марине', 'CONTRADICTION'),
    ('Позвонить Светлане', 'Позвонить Марине', 'CONTRADICTION'),
    ('Позвонить Ярославу', 'Написать неизвестному человеку', 'UNVERIFIED'),
    ('Оплатить подписку', 'Не оплатить подписку', 'CONTRADICTION'),
    ('Отменить приём', 'Назначить приём', 'CONTRADICTION'),
])
def test_reused_guard_contract(query, title, state):
    assert constraint_assessment(query, {'title': title, 'status': 'pending'})['state'] == state


def test_exact_paraphrase_name_negation_no_match_and_recurrence():
    exact = event('Позвонить Светлане')
    paraphrase = event('Связаться со Светланой')
    wrong = event('Позвонить Марине')
    opposite = event('Не позвонить Светлане')
    missing = event('Позвонить неизвестному человеку')
    result, service, _ = search([exact], [paraphrase, wrong, opposite, missing], 'Позвонить Светлане')
    assert {e.id for e in result} == {exact.id, paraphrase.id}
    assert service.result.unverified_count == 1 and service.result.mode == 'hybrid'
    assert service.result.states[str(wrong.id)] == 'REJECTED'
    assert search([], [], 'телескоп')[0] == []
    first = event('Разминка', recurrence_rule='FREQ=DAILY', recurrence_series_id=uuid4())
    second = event('Разминка', recurrence_rule=first.recurrence_rule,
                   recurrence_series_id=first.recurrence_series_id)
    done = event('Разминка', recurrence_rule=first.recurrence_rule,
                 recurrence_series_id=first.recurrence_series_id)
    done.status = 'completed'
    ranked, _, _ = search([first, second, done], [second, first], 'Разминка')
    assert len(ranked) == 2 and {e.status for e in ranked} == {'pending', 'completed'}


def test_vector_failure_savepoint_fallback_and_flag_off(caplog):
    item = event('Отчёт')
    error = OperationalError('SECRET SQL', {}, Exception('SECRET password'))
    result, service, _ = search([item], [], 'Отчёт', error=error)
    assert result == [item] and service.result.mode == 'lexical_fallback'
    assert 'SECRET' not in caplog.text
    result, _, repo = search([item], [], 'Отчёт', enabled=False)
    assert result == [item]
    repo.semantic.assert_not_called()
    repo.session.begin_nested.assert_not_called()


def test_shared_sql_filters_and_profile_not_global():
    repo = EventRepository(Mock(), 50)
    command = SearchEventsCommand(query='к врачу', start=date(2026, 10, 1), end=date(2026, 10, 31),
                                  event_type='MEETING', status='cancelled', recurrence_required=True)
    sql = str(repo.search_filters(select(Event), command).compile(dialect=postgresql.dialect()))
    for fragment in ['events.status !=', 'events.status =', 'events.event_date >=',
                     'events.event_date <=', 'events.type =', 'events.recurrence_series_id IS NOT NULL']:
        assert fragment in sql
    assert 'user_id' not in sql  # Agreed isolation boundary: one owner per database.
    repo.session.execute.return_value = []
    repo.semantic(command, unit(), EmbeddingProfile(), config())
    query = repo.session.execute.call_args.args[0]
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert 'embedding_cache.embedding <=>' in sql and 'source_title = events.title' in sql
    assert 'text-embedding-3-large' in compiled.params.values()
    assert 1536 in compiled.params.values() and 'cancelled' in compiled.params.values()


def test_canonical_date_is_sql_filter_not_an_invented_title_number():
    parsed = ParsedCommand(intent='SEARCH_EVENTS', period_expression='2026-10-16')
    query = literal_query('Найди Позвонить Светлане 16.10.2026', parsed)
    assert query == 'Позвонить Светлане'
    assert '42' in literal_query('Найди заказ 42 16.10.2026', parsed)


@compiles(JSONB, 'sqlite')
def sqlite_json(element, compiler, **kwargs):
    return 'JSON'


@pytest.fixture
def chat(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Event.__table__.create(engine)
    Conversation.__table__.create(engine)
    provider = FakeLLMProvider({})
    settings = config()
    embeddings = FakeEmbeddingProvider({'Позвонить Светлане': unit(), 'Отчёт': unit(1)})
    # The real PostgreSQL implementations have a separate integration test below.
    # This endpoint smoke verifies dialogue, transaction replay, guard and persistence.
    def lexical(self, command, **kwargs):
        return [e for e in self.session.scalars(self.search_filters(select(Event), command))
                if e.title.casefold() == command.query.casefold()]
    def semantic(self, command, vector, profile, settings):
        assert profile == EmbeddingProfile('text-embedding-3-large')
        return list(self.session.scalars(self.search_filters(select(Event), command)))
    monkeypatch.setattr(EventRepository, 'search', lexical)
    monkeypatch.setattr(EventRepository, 'semantic', semantic)
    monkeypatch.setattr(routes, 'get_settings', lambda: settings)
    def embed(texts):
        # Check same API Session: network phase must be outside its transaction.
        assert active and not active[-1].in_transaction()
        return original(texts)
    original = embeddings.embed
    embeddings.embed = embed
    original_execute = execute_message
    monkeypatch.setattr(routes, 'execute_message', lambda *args: original_execute(
        *args, embedding_provider=embeddings))
    active = []
    def session():
        with Session(engine, expire_on_commit=False) as db:
            active.append(db)
            yield db
            active.pop()
    old_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_provider] = lambda: provider
    try:
        with TestClient(app) as client:
            yield client, provider, embeddings, engine, settings
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        engine.dispose()


def test_chat_endpoint_transaction_replay_and_uncertainty(chat):
    client, parser, embeddings, engine, settings = chat
    with Session(engine) as db, db.begin():
        db.add_all([event('Позвонить Светлане'), event('Позвонить Марине'),
                    event('Позвонить неизвестному человеку')])
    message = 'Найди Позвонить Светлане'
    parser.commands[message] = dict(intent='SEARCH_EVENTS', search_query='Позвонить Светлане')
    response = client.post('/api/v1/assistant/messages', json={'message': message})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['search_mode'] == 'hybrid' and body['unverified_count'] == 1
    assert [e['title'] for e in body['events']] == ['Позвонить Светлане']
    with Session(engine) as db:
        conversation = db.get(Conversation, UUID(body['conversation_id']))
        assert len(conversation.context['last_listed_events']) == 1
    embeddings.failures.append(EmbeddingError('TIMEOUT', retryable=True))
    response = client.post('/api/v1/assistant/messages', json={'message': message})
    assert response.json()['search_mode'] == 'lexical_fallback'
    assert len(embeddings.calls) == 2  # No automatic retry/cost multiplication.
    settings.semantic_search_enabled = False
    response = client.post('/api/v1/assistant/messages', json={'message': message})
    assert len(embeddings.calls) == 2 and response.json()['events']


def test_chat_literal_query_cannot_inject_model_facts_and_status_filter(chat):
    client, parser, embeddings, engine, settings = chat
    cancelled = event('Обсуждение проекта', event_date=date(2026, 10, 25))
    cancelled.type, cancelled.status = 'meeting', 'cancelled'
    deleted = event('Удалённое', event_date=date(2026, 10, 25))
    deleted.type, deleted.status = 'meeting', 'deleted'
    with Session(engine) as db, db.begin():
        db.add_all([cancelled, deleted])
    message = 'Покажи все отменённые встречи'
    parser.commands[message] = dict(intent='LIST_EVENTS', event_type='MEETING')
    body = client.post('/api/v1/assistant/messages', json={'message': message}).json()
    assert [e['status'] for e in body['events']] == ['cancelled']
    assert not body['requires_input'] and embeddings.calls == []
    message = 'Найди Отчёт'
    parser.commands[message] = dict(intent='SEARCH_EVENTS', search_query='Отчёт Светланы')
    client.post('/api/v1/assistant/messages', json={'message': message})
    assert embeddings.calls == [['Отчёт']]


def test_chat_recurring_listing_groups_real_series_and_completed_without_date(chat):
    client, parser, embeddings, engine, settings = chat
    series = uuid4()
    with Session(engine) as db, db.begin():
        db.add_all([
            event('Разминка', event_date=date(2026, 10, 25), recurrence_rule='FREQ=DAILY',
                  recurrence_series_id=series),
            event('Разминка', event_date=date(2026, 10, 26), recurrence_rule='FREQ=DAILY',
                  recurrence_series_id=series),
            event('Разминка', event_date=date(2026, 10, 25)),
        ])
        completed = event('Прочитать статью')
        completed.status = 'completed'
        db.add(completed)
    message = 'Найди повторяющиеся задачи'
    parser.commands[message] = dict(intent='SEARCH_EVENTS', search_query='повторяющиеся задачи',
                                    event_type='TASK')
    body = client.post('/api/v1/assistant/messages', json={'message': message}).json()
    assert len(body['events']) == 1 and body['events'][0]['recurrence_series_id'] == str(series)
    assert body['events'][0]['event_date'] == '2026-10-25'
    message = 'Покажи все выполненные задачи'
    parser.commands[message] = dict(intent='LIST_EVENTS', event_type='TASK')
    body = client.post('/api/v1/assistant/messages', json={'message': message}).json()
    assert [e['title'] for e in body['events']] == ['Прочитать статью']
    assert not embeddings.calls


def test_embedding_phase_rejects_concurrent_dialogue_change(chat):
    client, parser, embeddings, engine, settings = chat
    with Session(engine) as db, db.begin():
        conversation = Conversation()
        db.add(conversation)
        db.flush()
        identity = str(conversation.id)
    message = 'Найди Отчёт'
    parser.commands[message] = dict(intent='SEARCH_EVENTS', search_query='Отчёт')
    def concurrent_write(texts):
        with Session(engine) as db, db.begin():
            db.get(Conversation, UUID(identity)).context = {'last_event_scope': 'last_created'}
    embeddings.on_call = concurrent_write
    response = client.post('/api/v1/assistant/messages', json={
        'message': message, 'conversation_id': identity})
    assert response.status_code == 503 and 'изменился' in response.json()['detail']


def test_synthetic_backend_starts_on_real_tcp_and_reports_version(chat):
    import socket
    from threading import Event as StopEvent
    from threading import Thread
    from time import monotonic

    import httpx
    import uvicorn

    client, parser, embeddings, engine, settings = chat
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 5
        while not server.started and thread.is_alive() and monotonic() < deadline:
            StopEvent().wait(.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}') as tcp:
            health = tcp.get('/health')
            assert health.status_code == 200
            assert health.json()['database'] == 'ok' and health.json()['test_mode'] is True
            assert health.json()['code_version'] == health.json()['source_version']
            message = 'Найди Отчёт'
            parser.commands[message] = dict(intent='SEARCH_EVENTS', search_query='Отчёт')
            result = tcp.post('/api/v1/assistant/messages', json={'message': message})
            assert result.status_code == 200 and result.json()['search_mode'] == 'hybrid'
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


def test_primary_worker_requires_explicit_pinned_approval_before_connect():
    engine = create_engine('postgresql+psycopg://unused:unused@127.0.0.1:5432/assistant')
    with pytest.raises(IndexingTargetError):
        verify_worker_target(engine, Settings(_env_file=None))
    with pytest.raises(IndexingTargetError):
        verify_worker_target(engine, Settings(_env_file=None,
            embedding_production_indexing_approved=True, embedding_allowed_cluster_id='wrong'))
    engine.dispose()


@pytest.mark.integration
def test_postgresql_actual_worker_vector_fts_filters_and_incremental(db, integration_engine):
    """Destructive fixture is strictly disposable-gated in conftest before any write."""
    import os

    from app.services.indexing import IndexingWorker
    settings = config(embedding_worker_enabled=True,
                      embedding_allowed_cluster_id=os.environ['EMBEDDING_ALLOWED_CLUSTER_ID'])
    item = event('Позвонить Светлане', event_date=date(2026, 10, 25), event_time=time(15))
    db.add(item)
    db.commit()
    vectors = FakeEmbeddingProvider({'Позвонить Светлане': unit(), 'Связаться со Светланой': unit()})
    worker = IndexingWorker(integration_engine, vectors, settings)
    assert worker.run_once()['completed'] == 1
    assert worker.run_once()['provider_calls'] == 0
    prepared = PreparedProvider()
    prepared.embedding_test_provider = vectors
    prepared.embedding_request, prepared.embedding_vector = 'Связаться со Светланой', unit()
    service = HybridSearch(EventRepository(db, 50), settings, prepared)
    query = SearchEventsCommand(query='Связаться со Светланой')
    assert [e.id for e in service.execute(query)] == [item.id]
    assert service.result.mode == 'hybrid'
    assert service.repository.search(SearchEventsCommand(query='Светлане'))
    item.status = 'completed'
    db.commit()
    assert service.repository.semantic(query.model_copy(update={'status': 'cancelled'}), unit(),
                                       vectors.profile, settings) == []
    assert service.repository.semantic(query.model_copy(update={'status': 'completed'}), unit(),
                                       vectors.profile, settings)
    item.title = 'Купить телескоп'
    db.commit()
    assert service.repository.semantic(query, unit(), vectors.profile, settings) == []
    assert worker.run_once()['completed'] == 1
    item.status = 'deleted'
    db.commit()
    assert service.execute(query) == []
    assert worker.run_once()['removed_stale'] >= 0
